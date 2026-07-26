/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */
/* atsc_fs_checker with 313-segment spacing validation (see README, Tier 21). */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_fs_checker_inst_impl.h"
#include "atsc_pnXXX_impl.h"
#include "atsc_syminfo_impl.h"
#include "atsc_types.h"
#include <gnuradio/dtv/atsc_consts.h>
#include <gnuradio/io_signature.h>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <chrono>

#define ATSC_SEGMENTS_PER_DATA_FIELD 313

using gr::dtv::ATSC_DATA_SEGMENT_LENGTH;
using gr::dtv::plinfo;

// PN511/PN63 error tolerances. Raised from upstream gr-dtv (20/5) to (50/15)
// so synthetic IQ at SNR=25 dB AWGN with residual SRRC ISI still trains the
// equalizer (otherwise rs_clean_frac collapses to 0%). Real RF lock at 99.1%.
// 2026-07-05: env-overridable for RESCUE decoding — offline replay of a
// canyon capture (RF7: 27 dB ripple kills FS detection outright) can
// afford to accept risky syncs a live chain must reject. Defaults intact.
static const int PN511_ERROR_LIMIT = []() -> int {
    if (const char* p = std::getenv("ATSCPLUS_PN511_LIMIT")) {
        int v = std::atoi(p);
        if (v >= 10 && v <= 220) return v;
    }
    return 50;
}();
static const int PN63_ERROR_LIMIT = []() -> int {
    if (const char* p = std::getenv("ATSCPLUS_PN63_LIMIT")) {
        int v = std::atoi(p);
        if (v >= 3 && v <= 30) return v;
    }
    return 15;
}();

namespace gr {
namespace atscplus {

atsc_fs_checker_inst::sptr atsc_fs_checker_inst::make()
{
    return gnuradio::make_block_sptr<atsc_fs_checker_inst_impl>();
}

atsc_fs_checker_inst_impl::atsc_fs_checker_inst_impl()
    : gr::block("atscplus_atsc_fs_checker_inst",
                // in1 = OPTIONAL complex companion (from sync out1). out2 =
                // OPTIONAL complex segments carried through the SAME drop/keep
                // decisions as the real path (widely-linear feed, 2026-07-26).
                // Existing chains connect in0/out0/out1 only and are unaffected.
                gr::io_signature::make2(1,
                                        2,
                                        ATSC_DATA_SEGMENT_LENGTH * sizeof(float),
                                        ATSC_DATA_SEGMENT_LENGTH * sizeof(gr_complex)),
                gr::io_signature::make3(2,
                                        3,
                                        ATSC_DATA_SEGMENT_LENGTH * sizeof(float),
                                        sizeof(plinfo),
                                        ATSC_DATA_SEGMENT_LENGTH * sizeof(gr_complex)))
{
    reset();
}

void atsc_fs_checker_inst_impl::reset()
{
    d_index = 0;
    std::memset(d_sample_sr, 0, sizeof(d_sample_sr));
    std::memset(d_tag_sr, 0, sizeof(d_tag_sr));
    std::memset(d_bit_sr, 0, sizeof(d_bit_sr));
    d_field_num = 0;
    d_segment_num = 0;

    d_fs_validate_enabled = true;
    if (const char* s = std::getenv("ATSCPLUS_FS_VALIDATE")) {
        if (*s && (std::strcmp(s, "0") == 0 || std::strcmp(s, "off") == 0 ||
                   std::strcmp(s, "OFF") == 0 || std::strcmp(s, "false") == 0)) {
            d_fs_validate_enabled = false;
        }
    }
    d_fs_tol_low  = 280;
    d_fs_tol_high = INT_MAX;
    if (const char* s = std::getenv("ATSCPLUS_FS_TOL_LOW")) {
        int v = std::atoi(s);
        if (v > 0) d_fs_tol_low = v;
    }
    if (const char* s = std::getenv("ATSCPLUS_FS_TOL_HIGH")) {
        int v = std::atoi(s);
        if (v > 0) d_fs_tol_high = v;
    }
    d_fs_locked = false;
    d_segs_since_accepted_fs = 0;
    d_fs_accepted = 0;
    d_fs_rejected_early = 0;
    d_fs_rejected_late = 0;
    d_coast_run = 0;
    d_coast_total = 0;
    d_clean_fs_streak = 0;
}

atsc_fs_checker_inst_impl::~atsc_fs_checker_inst_impl()
{
    std::fprintf(stderr,
                 "[fs_check] accepted=%llu rejected_early=%llu rejected_late=%llu "
                 "coasted=%llu validate=%s tol=[%d,%d]\n",
                 (unsigned long long)d_fs_accepted,
                 (unsigned long long)d_fs_rejected_early,
                 (unsigned long long)d_fs_rejected_late,
                 (unsigned long long)d_coast_total,
                 d_fs_validate_enabled ? "ON" : "OFF",
                 d_fs_tol_low, d_fs_tol_high);
}

int atsc_fs_checker_inst_impl::general_work(int noutput_items,
                                            gr_vector_int& ninput_items,
                                            gr_vector_const_void_star& input_items,
                                            gr_vector_void_star& output_items)
{
    auto in = static_cast<const float*>(input_items[0]);
    auto out = static_cast<float*>(output_items[0]);
    auto out_pl = static_cast<plinfo*>(output_items[1]);
    // OPTIONAL complex companion — carried through the identical drop/keep path
    const gr_complex* in_c =
        (input_items.size() > 1) ? static_cast<const gr_complex*>(input_items[1]) : nullptr;
    gr_complex* out_c =
        (output_items.size() > 2) ? static_cast<gr_complex*>(output_items[2]) : nullptr;

    // Drought-forensics telemetry (gated; zero overhead unless ATSCPLUS_FS_TELEM=1).
    // Logs field-sync GAP anomalies (gap != 313 = a segment-count slip) and
    // rejections, with a steady-clock timestamp, so a drought (noise in the TS)
    // can be correlated against fs_check losing segment alignment. If gaps stay
    // 313 and there are no rejects through a drought, the corruption is
    // downstream of fs_check (deinterleaver/RS), not here.
    static const bool FS_TELEM = []() {
        const char* p = std::getenv("ATSCPLUS_FS_TELEM"); return p && std::atoi(p) != 0;
    }();
    static const auto fs_t0 = std::chrono::steady_clock::now();
    auto fs_now = [&]() {
        return std::chrono::duration<double>(
                   std::chrono::steady_clock::now() - fs_t0).count();
    };

    // ── DROUGHT-RECOVERY FIX (2026-06-07) ───────────────────────────────────
    // Root cause (proven by fs_telem): an OsO sample-drop disrupts field-sync
    // spacing; real field syncs then arrive at gap != 313 and the 313-spacing
    // validation REJECTS them. d_fs_locked is never cleared during operation,
    // so the rejection is permanent — gap climbs into the thousands (one run hit
    // 12503 ≈ 40 fields) and the TS output is noise the whole time = the drought.
    // Fix: if we go FS_RELOCK_SEGS segments without accepting a field sync, the
    // lock is clearly stale — drop it so the next real PN511 field sync re-
    // acquires regardless of spacing (the same path used at cold start). The
    // equalizer stays healthy through droughts, so the re-acquire locks onto a
    // CLEAN field sync. In normal operation the gap never exceeds ~626, so this
    // never fires → zero effect when there's no drought. 0 disables (A/B).
    static const int FS_RELOCK_SEGS = []() -> int {
        if (const char* p = std::getenv("ATSCPLUS_FS_RELOCK_SEGS")) {
            int v = std::atoi(p);
            if (v >= 0) return v;
        }
        return 939;   // 3 fields; a single missed FS self-corrects by ~626
    }();

    // ── SYNC FLYWHEEL (2026-07-05, Strike 1) ───────────────────────────────
    // Proven by the 3-way trial: midday glitches are NOT an equalizer
    // disease — impulses corrupt the field-sync marker past the PN error
    // limit, the gate runs 313 segments without an accepted FS, declares
    // FIELD-LOSS and HALTS ALL OUTPUT until the next accepted sync. One
    // microsecond of impulse costs a full field (~24 ms) minimum. But the
    // transmitter's clock never flinched: if the FS is missed, we know
    // EXACTLY where it should have been — synthesize it, alternate the
    // field parity, keep emitting. Bounded by FS_COAST consecutive fields
    // so a true signal loss still halts honestly (liveness law). 0 = off.
    static const int FS_COAST = []() -> int {
        if (const char* p = std::getenv("ATSCPLUS_FS_COAST")) {
            int v = std::atoi(p);
            if (v >= 0 && v <= 100) return v;
        }
        return 0;
    }();
    // v2 WARMUP GATE (trial 1 lesson: 8 coasts in the first 10 s were on
    // pre-lock startup garbage and poisoned downstream alignment for the
    // whole run). The flywheel may only engage after the link has proven
    // real, consecutive, on-schedule field syncs — i.e. it protects an
    // ESTABLISHED rhythm, never invents one.
    static const int FS_COAST_WARMUP = []() -> int {
        if (const char* p = std::getenv("ATSCPLUS_FS_COAST_WARMUP")) {
            int v = std::atoi(p);
            if (v >= 1 && v <= 10000) return v;
        }
        return 80;   // ~2 s of consecutive clean 313-spaced syncs
    }();

    int output_produced = 0;

    for (int i = 0; i < noutput_items; i++) {
        d_segs_since_accepted_fs++;

        // Re-acquire after a prolonged lock-loss (drought): drop the lock so the
        // spacing validation is bypassed and the next real field sync re-locks.
        if (FS_RELOCK_SEGS > 0 && d_fs_locked &&
            (int)d_segs_since_accepted_fs > FS_RELOCK_SEGS) {
            d_fs_locked = false;
            if (FS_TELEM)
                std::fprintf(stderr,
                    "[fs_telem t=%8.2fs] RE-ACQUIRE (no FS for %llu segs > %d) — "
                    "dropping lock to recover\n",
                    fs_now(), (unsigned long long)d_segs_since_accepted_fs,
                    FS_RELOCK_SEGS);
        }

        int errors_511 = 0;
        for (int j = 0; j < LENGTH_511; j++) {
            errors_511 +=
                (in[i * ATSC_DATA_SEGMENT_LENGTH + j + OFFSET_511] >= 0) ^ atsc_pn511[j];
        }

        if (errors_511 < PN511_ERROR_LIMIT) {
            int errors_63 = 0;
            for (int j = 0; j < LENGTH_2ND_63; j++)
                errors_63 += (in[i * ATSC_DATA_SEGMENT_LENGTH + j + OFFSET_2ND_63] >= 0) ^
                             atsc_pn63[j];

            int candidate_field = 0;
            if (errors_63 <= PN63_ERROR_LIMIT) {
                candidate_field = 1;
            } else if (errors_63 >= (LENGTH_2ND_63 - PN63_ERROR_LIMIT)) {
                candidate_field = 2;
            }

            if (candidate_field != 0) {
                bool accept = true;
                uint64_t gap = d_segs_since_accepted_fs;
                if (d_fs_validate_enabled && d_fs_locked) {
                    if ((int)gap < d_fs_tol_low) {
                        accept = false;
                        d_fs_rejected_early++;
                        if (FS_TELEM)
                            std::fprintf(stderr,
                                "[fs_telem t=%8.2fs] REJECT-EARLY field=%d gap=%llu (tol_low=%d)\n",
                                fs_now(), candidate_field,
                                (unsigned long long)gap, d_fs_tol_low);
                    } else if ((int)gap > d_fs_tol_high) {
                        accept = false;
                        d_fs_rejected_late++;
                        if (FS_TELEM)
                            std::fprintf(stderr,
                                "[fs_telem t=%8.2fs] REJECT-LATE field=%d gap=%llu (tol_high=%d)\n",
                                fs_now(), candidate_field,
                                (unsigned long long)gap, d_fs_tol_high);
                    }
                }

                if (accept) {
                    // Log gap anomalies: a clean field is exactly 313 segments.
                    // Any other gap = a segment-count slip (the OsO fingerprint).
                    if (FS_TELEM && d_fs_locked && gap != ATSC_SEGMENTS_PER_DATA_FIELD)
                        std::fprintf(stderr,
                            "[fs_telem t=%8.2fs] GAP-ANOMALY field=%d gap=%llu (expected %d)\n",
                            fs_now(), candidate_field,
                            (unsigned long long)gap, ATSC_SEGMENTS_PER_DATA_FIELD);
                    d_field_num = candidate_field;
                    d_segment_num = -1;
                    d_fs_accepted++;
                    d_fs_locked = true;
                    // warmup bookkeeping: only an exactly-on-schedule sync
                    // extends the clean streak; anything else restarts it
                    if (gap == ATSC_SEGMENTS_PER_DATA_FIELD)
                        d_clean_fs_streak++;
                    else
                        d_clean_fs_streak = 0;
                    d_segs_since_accepted_fs = 0;
                    d_coast_run = 0;   // real sync seen — flywheel rearms
                }
            }
        }

        if (d_field_num == 1 || d_field_num == 2) {
            std::memcpy(&out[output_produced * ATSC_DATA_SEGMENT_LENGTH],
                        &in[i * ATSC_DATA_SEGMENT_LENGTH],
                        ATSC_DATA_SEGMENT_LENGTH * sizeof(float));
            // mirror the complex companion at the SAME output index (stays
            // aligned through normal/coast/halt — all key on output_produced)
            if (out_c && in_c)
                std::memcpy(&out_c[output_produced * ATSC_DATA_SEGMENT_LENGTH],
                            &in_c[i * ATSC_DATA_SEGMENT_LENGTH],
                            ATSC_DATA_SEGMENT_LENGTH * sizeof(gr_complex));

            plinfo pli_out;
            // set_regular_seg(field2, segno) — the GR stock equalizer keys on
            // segno==-1 to detect sync segments and train its taps. Calling
            // set_field_sync1/2 leaves segno=0, the equalizer never trains,
            // and stock regresses from 99.1% to 0.3% RS-clean.
            pli_out.set_regular_seg((d_field_num == 2), d_segment_num);

            d_segment_num++;
            if (d_segment_num > (ATSC_SEGMENTS_PER_DATA_FIELD - 1)) {
                if (FS_COAST > 0 && d_coast_run < FS_COAST
                        && d_clean_fs_streak >= FS_COAST_WARMUP) {
                    // FLYWHEEL: the field sync belongs exactly HERE — the
                    // detector missed it (impulse-chewed PN), but timing
                    // didn't move. Synthesize it: alternate field parity,
                    // restart the segment count, and emit this segment AS
                    // the sync segment (it IS the real FS segment when
                    // timing held; the equalizer trains on it as usual).
                    d_coast_run++;
                    d_coast_total++;
                    d_field_num = (d_field_num == 1) ? 2 : 1;
                    d_segment_num = -1;
                    if (FS_TELEM)
                        std::fprintf(stderr,
                            "[fs_telem t=%8.2fs] COAST #%d (synthetic FS, "
                            "field=%d, lifetime=%llu)\n",
                            fs_now(), d_coast_run, d_field_num,
                            (unsigned long long)d_coast_total);
                    plinfo pli_fs;
                    pli_fs.set_regular_seg((d_field_num == 2), d_segment_num);
                    d_segment_num++;
                    out_pl[output_produced++] = pli_fs;
                } else {
                    // Ran a full field without a real FS and the coast
                    // budget is spent → genuine alignment loss; halt output
                    // until the next accepted FS (honest silence).
                    if (FS_TELEM)
                        std::fprintf(stderr,
                            "[fs_telem t=%8.2fs] FIELD-LOSS (313 segs, no new FS "
                            "accepted%s)\n", fs_now(),
                            FS_COAST > 0 ? ", coast budget spent" : "");
                    d_field_num = 0;
                    d_segment_num = 0;
                }
            } else {
                out_pl[output_produced++] = pli_out;
            }
        }
    }

    consume_each(noutput_items);
    return output_produced;
}

} /* namespace atscplus */
} /* namespace gr */
