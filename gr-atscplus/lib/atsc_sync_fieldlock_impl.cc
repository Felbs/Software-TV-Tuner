/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Field-Sync-anchored ATSC segment-sync detector.
 *
 * Inherits the soft 4-tap matched filter + EMA-bin picker from
 * atsc_sync_soft (which yields ~91% segment alignment on weak captures
 * because brief signal fades make the +5,-5,-5,+5 pattern indistinguishable
 * from noise; ~199 lock-loss/relock cycles over 5000 segments).
 *
 * Adds an ~641-tap Field Sync template correlator that runs once per
 * emitted segment. The FS pattern is highly deterministic and ~30 dB
 * stronger as a per-segment match than the 4-tap segment-sync template,
 * so FS detections survive fades that break 4-tap. When FS is detected,
 * (a) the argmax offset corrects bin-position misalignment, and (b) FS
 * lock holds the segment lock through subsequent 4-tap fades for up to
 * d_fs_hold segments.
 *
 * See ~/overnight/TIMING_BUILD_RESULT.md for the diagnosis that motivated
 * this block.
 */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_sync_fieldlock_impl.h"
#include "atsc_pnXXX_impl.h"
#include <gnuradio/io_signature.h>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

using namespace gr::dtv;

namespace gr {
namespace atscplus {

static const double LOOP_FILTER_TAP = 0.0005;
static const double ADJUSTMENT_GAIN = 1.0e-5 / (10 * ATSC_DATA_SEGMENT_LENGTH);
static const int SYMBOL_INDEX_OFFSET = 3;

atsc_sync_fieldlock::sptr atsc_sync_fieldlock::make(float rate)
{
    return gnuradio::make_block_sptr<atsc_sync_fieldlock_impl>(rate);
}

atsc_sync_fieldlock_impl::atsc_sync_fieldlock_impl(float rate)
    : gr::block("atscplus_atsc_sync_fieldlock",
                io_signature::make(1, 1, sizeof(float)),
                io_signature::make(1, 1, ATSC_DATA_SEGMENT_LENGTH * sizeof(float))),
      d_rx_clock_to_symbol_freq(rate / ATSC_SYMBOL_RATE),
      d_si(0)
{
    d_loop.set_taps(LOOP_FILTER_TAP);

    // Soft-detector defaults: same as atsc_sync_soft / kalman.
    d_alpha = 0.40f;
    d_lock_threshold = 4.0f;
    d_unlock_threshold = 2.0f;
    d_sticky_fraction = 0.95f;
    d_emit_when_unlocked = true;
    d_debug = false;
    d_locked_idx = -1;
    d_timing_gain_scale = 1.0f;
    d_local_move_factor = 1.10f;
    d_search_w = 6;

    // Field-Sync detector defaults. Tuned on sdr_RF36.cf32:
    // - True FS correlation peaks at ~3200 (641 active * ~5 mag); set
    //   threshold at 2500 to comfortably reject noise (peak ~2000 in
    //   transients) while catching real FS through partial fades.
    // - Search window of 50 samples covers most observed bin-jitter (the
    //   misaligned-segment offset distribution shows ~30% within ±3,
    //   so ±50 is conservative). Wider search costs ~ms per replay run.
    // - 626-segment hold (2 fields) bridges single-FS misses. Diagnostic
    //   confirmed gap=602 segs occurred once in a 5000-seg run.
    // - FS_DRIVE off by default: FS-derived bin pinning between FSes
    //   ignores real drift, which COSTS alignment (40% with drive on
    //   vs 89% baseline). Drive only helps the moment of FS detection.
    d_fs_window_w = 50;
    d_fs_threshold = 2500.0f;
    d_fs_hold = 626;
    d_fs_drive = false;
    d_fs_hold_lock = true;
    d_fs_correct = true;

    auto getf = [](const char* k, float lo, float hi, float& dst) {
        if (const char* p = std::getenv(k)) {
            float v = std::atof(p);
            if (v >= lo && v <= hi) dst = v;
        }
    };
    auto geti = [](const char* k, int lo, int hi, int& dst) {
        if (const char* p = std::getenv(k)) {
            int v = std::atoi(p);
            if (v >= lo && v <= hi) dst = v;
        }
    };
    auto getb = [](const char* k, bool& dst) {
        if (const char* p = std::getenv(k)) dst = (std::atoi(p) != 0);
    };

    getf("ATSC_SYNC_FL_ALPHA", 0.0f, 1.0f, d_alpha);
    getf("ATSC_SYNC_FL_LOCK", 0.0f, 100.0f, d_lock_threshold);
    getf("ATSC_SYNC_FL_UNLOCK", 0.0f, 100.0f, d_unlock_threshold);
    getf("ATSC_SYNC_FL_STICKY", 0.0f, 1.0f, d_sticky_fraction);
    getf("ATSC_SYNC_FL_TIMING_SCALE", 0.0f, 10.0f, d_timing_gain_scale);
    getf("ATSC_SYNC_FL_LOCAL_MOVE", 1.0f, 10.0f, d_local_move_factor);
    geti("ATSC_SYNC_FL_SEARCH_W", 1, 416, d_search_w);

    geti("ATSC_SYNC_FL_FS_W", 0, 50, d_fs_window_w);
    getf("ATSC_SYNC_FL_FS_THR", 0.0f, 1e8f, d_fs_threshold);
    geti("ATSC_SYNC_FL_FS_HOLD", 0, 100000, d_fs_hold);
    getb("ATSC_SYNC_FL_FS_DRIVE", d_fs_drive);
    getb("ATSC_SYNC_FL_FS_HOLDLOCK", d_fs_hold_lock);
    getb("ATSC_SYNC_FL_FS_CORRECT", d_fs_correct);

    getb("ATSC_SYNC_FL_EMIT_UNLOCKED", d_emit_when_unlocked);
    getb("ATSC_SYNC_FL_DEBUG", d_debug);

    d_segs_emitted = 0;
    d_segs_held = 0;
    d_segs_aligned = 0;
    d_segs_total = 0;
    d_relocks = 0;
    d_seg_count = 0;
    d_fs_detections = 0;
    d_fs_corrections = 0;
    d_fs_held_locks = 0;
    d_fs_drive_overrides = 0;

    build_fs_template();

    std::fprintf(stderr,
                 "[sync_fieldlock] rate=%.0f alpha=%.4f lock=%.2f unlock=%.2f "
                 "sticky=%.2f timing_scale=%.2f local_move=%.2f search_w=%d "
                 "fs_w=%d fs_thr=%.2f fs_hold=%d fs_drive=%d fs_holdlock=%d "
                 "fs_correct=%d fs_active=%zu emit_unlocked=%d debug=%d\n",
                 rate, d_alpha, d_lock_threshold, d_unlock_threshold,
                 d_sticky_fraction, d_timing_gain_scale, d_local_move_factor,
                 d_search_w, d_fs_window_w, d_fs_threshold, d_fs_hold,
                 (int)d_fs_drive, (int)d_fs_hold_lock, (int)d_fs_correct,
                 d_fs_active_idx.size(),
                 (int)d_emit_when_unlocked, (int)d_debug);

    reset();
}

void atsc_sync_fieldlock_impl::build_fs_template()
{
    // Initialize all positions to zero (default = ignore).
    for (int i = 0; i < FS_TPL_LEN; i++) d_fs_template[i] = 0.0f;

    // Positions 0-3: segment-sync pattern +5,-5,-5,+5 → template ±1.
    d_fs_template[0] = +1.0f;
    d_fs_template[1] = -1.0f;
    d_fs_template[2] = -1.0f;
    d_fs_template[3] = +1.0f;

    // Positions 4..514: PN511. atsc_pn511[k] is bit-valued (0/1);
    // map 1 → +1 (positive expected), 0 → -1 (negative expected) — same
    // convention as atsc_fs_checker_inst (`(sample >= 0) ^ atsc_pn511[k]`).
    for (int k = 0; k < 511; k++) {
        d_fs_template[4 + k] = (atsc_pn511[k] != 0) ? +1.0f : -1.0f;
    }

    // Positions 515..577: first PN63 (same in both fields).
    for (int k = 0; k < 63; k++) {
        d_fs_template[515 + k] = (atsc_pn63[k] != 0) ? +1.0f : -1.0f;
    }

    // Positions 578..640: middle PN63 — sign-flips between Field 1 and 2.
    // Leave at zero so template is field-agnostic.

    // Positions 641..703: third PN63 (same in both fields).
    for (int k = 0; k < 63; k++) {
        d_fs_template[641 + k] = (atsc_pn63[k] != 0) ? +1.0f : -1.0f;
    }

    // Positions 704..727 (VSB mode bits) and 728..831 (reserved+precode):
    // mode bits encode 8VSB modulation type as a (24,3) trellis-decodable
    // sequence; reserved tail includes a 12-symbol data-dependent precode
    // suffix. For robustness across captures, leave both regions at zero.

    // Cache nonzero positions for tight inner loop.
    d_fs_active_idx.clear();
    d_fs_active_sign.clear();
    d_fs_active_idx.reserve(FS_TPL_LEN);
    d_fs_active_sign.reserve(FS_TPL_LEN);
    for (int i = 0; i < FS_TPL_LEN; i++) {
        if (d_fs_template[i] != 0.0f) {
            d_fs_active_idx.push_back(i);
            d_fs_active_sign.push_back(d_fs_template[i]);
        }
    }
}

void atsc_sync_fieldlock_impl::reset()
{
    d_w = d_rx_clock_to_symbol_freq;
    d_mu = 0.5;
    d_timing_adjust = 0;
    d_counter = 0;
    d_symbol_index = 0;
    d_seg_locked = false;
    d_locked_idx = -1;
    d_mf_idx = 0;
    memset(d_mf_buf, 0, sizeof(d_mf_buf));
    memset(d_sample_mem, 0, sizeof(d_sample_mem));
    memset(d_data_mem, 0, sizeof(d_data_mem));
    memset(d_integrator, 0, sizeof(d_integrator));
    d_fs_locked = false;
    d_fs_anchor_idx = -1;
    d_segs_since_fs = 0;
}

atsc_sync_fieldlock_impl::~atsc_sync_fieldlock_impl()
{
    double align_pct = (d_segs_emitted > 0)
        ? 100.0 * (double)d_segs_aligned / (double)d_segs_emitted
        : 0.0;
    std::fprintf(stderr,
                 "[sync_fieldlock FINAL] segs_emitted=%llu segs_held=%llu "
                 "segs_aligned=%llu (%.2f%%) relocks=%llu fs_detections=%llu "
                 "fs_corrections=%llu fs_held_locks=%llu fs_drive_overrides=%llu\n",
                 (unsigned long long)d_segs_emitted,
                 (unsigned long long)d_segs_held,
                 (unsigned long long)d_segs_aligned,
                 align_pct,
                 (unsigned long long)d_relocks,
                 (unsigned long long)d_fs_detections,
                 (unsigned long long)d_fs_corrections,
                 (unsigned long long)d_fs_held_locks,
                 (unsigned long long)d_fs_drive_overrides);
}

void atsc_sync_fieldlock_impl::forecast(int noutput_items,
                                        gr_vector_int& ninput_items_required)
{
    unsigned ninputs = ninput_items_required.size();
    for (unsigned i = 0; i < ninputs; i++)
        ninput_items_required[i] =
            static_cast<int>(noutput_items * d_rx_clock_to_symbol_freq *
                             ATSC_DATA_SEGMENT_LENGTH) + 1500 - 1;
}

bool atsc_sync_fieldlock_impl::fs_check(const float* seg,
                                        float& peak_corr,
                                        int& peak_offset)
{
    // Sweep offsets [-W, +W] using circular indexing. When the 4-tap
    // segment-sync detector is way off (e.g., locked to a wrong bin
    // ~100 samples from the true seg-sync), we still need to find FS
    // to re-anchor. Circular indexing treats d_data_mem as a 832-sample
    // ring; if FS template covers the whole segment, a circular shift
    // catches it regardless of where the bin lock landed.
    //
    // For W ≤ 416 the search covers the full segment. The FS template
    // is unique within a segment, so the true peak only fires at the
    // right shift; data bins give noise.
    const int W = d_fs_window_w;
    const int N = (int)d_fs_active_idx.size();
    const int* ai = d_fs_active_idx.data();
    const float* as = d_fs_active_sign.data();

    float best = -1e30f;
    int   best_off = 0;
    for (int off = -W; off <= W; off++) {
        float corr = 0.0f;
        for (int j = 0; j < N; j++) {
            int idx = ai[j] + off;
            if (idx < 0) idx += FS_TPL_LEN;
            else if (idx >= FS_TPL_LEN) idx -= FS_TPL_LEN;
            corr += as[j] * seg[idx];
        }
        if (corr > best) { best = corr; best_off = off; }
    }
    peak_corr = best;
    peak_offset = best_off;
    return best > d_fs_threshold;
}

int atsc_sync_fieldlock_impl::general_work(int noutput_items,
                                           gr_vector_int& ninput_items,
                                           gr_vector_const_void_star& input_items,
                                           gr_vector_void_star& output_items)
{
    const float* in = static_cast<const float*>(input_items[0]);
    float* out = static_cast<float*>(output_items[0]);

    float interp_sample;
    d_si = 0;
    d_output_produced = 0;

    while (d_output_produced < noutput_items &&
           (d_si + (int)d_interp.ntaps()) < ninput_items[0]) {

        interp_sample = d_interp.interpolate(&in[d_si], d_mu);
        const double t_gain = d_seg_locked
            ? (double)d_timing_gain_scale : 1.0;
        d_mu += t_gain * ADJUSTMENT_GAIN * 1e3 * d_timing_adjust;

        double s = d_mu + d_w;
        double float_incr = std::floor(s);
        d_mu = s - float_incr;
        d_incr = (int)float_incr;
        if (d_incr < 1) d_incr = 1;
        if (d_incr > 3) d_incr = 3;
        d_si += d_incr;

        d_sample_mem[d_counter] = interp_sample;

        d_mf_buf[d_mf_idx] = interp_sample;
        d_mf_idx = (d_mf_idx + 1) & 3;
        const float x_nm3 = d_mf_buf[d_mf_idx];
        const float x_nm2 = d_mf_buf[(d_mf_idx + 1) & 3];
        const float x_nm1 = d_mf_buf[(d_mf_idx + 2) & 3];
        const float x_n   = d_mf_buf[(d_mf_idx + 3) & 3];
        const float corr = x_nm3 - x_nm2 - x_nm1 + x_n;
        d_integrator[d_counter] =
            (1.0f - d_alpha) * d_integrator[d_counter] + d_alpha * corr;

        d_symbol_index++;
        if (d_symbol_index >= ATSC_DATA_SEGMENT_LENGTH) d_symbol_index = 0;

        d_counter++;
        if (d_counter >= ATSC_DATA_SEGMENT_LENGTH) {
            float best_val = d_integrator[0];
            int best_idx = 0;
            double sum_sq = 0.0;
            for (int i = 0; i < ATSC_DATA_SEGMENT_LENGTH; i++) {
                const float v = d_integrator[i];
                sum_sq += (double)v * (double)v;
                if (v > best_val) { best_val = v; best_idx = i; }
            }
            const double rms = std::sqrt(sum_sq / (double)ATSC_DATA_SEGMENT_LENGTH);
            const double snr_ratio = (rms > 1e-9) ? (double)best_val / rms : 0.0;

            // ---- Sticky-lock + constrained search around d_locked_idx ----
            if (d_seg_locked && d_locked_idx >= 0 &&
                d_locked_idx < ATSC_DATA_SEGMENT_LENGTH) {
                const int W = d_search_w;
                int local_best_idx = d_locked_idx;
                float local_best_val = d_integrator[d_locked_idx];
                for (int d = -W; d <= W; d++) {
                    int j = d_locked_idx + d;
                    if (j < 0) j += ATSC_DATA_SEGMENT_LENGTH;
                    if (j >= ATSC_DATA_SEGMENT_LENGTH) j -= ATSC_DATA_SEGMENT_LENGTH;
                    if (d_integrator[j] > local_best_val) {
                        local_best_val = d_integrator[j];
                        local_best_idx = j;
                    }
                }
                const float locked_val = d_integrator[d_locked_idx];
                if (locked_val >= d_sticky_fraction * best_val) {
                    if (local_best_idx != d_locked_idx &&
                        local_best_val >= d_local_move_factor * locked_val) {
                        d_locked_idx = local_best_idx;
                        best_idx = local_best_idx;
                        best_val = local_best_val;
                    } else {
                        best_idx = d_locked_idx;
                        best_val = locked_val;
                    }
                } else {
                    d_locked_idx = best_idx;
                    d_relocks++;
                }
            }

            // ---- FS-driven bin override ----
            // When FS lock is fresh, use the FS-confirmed bin as the
            // authoritative segment-sync position; ignore EMA pick. The
            // 4-tap EMA is too noisy on weak captures to be trusted
            // through a fade, but FS gives 30 dB more processing gain
            // on the segments where it appears.
            if (d_fs_drive && d_fs_locked && d_fs_anchor_idx >= 0) {
                if (best_idx != d_fs_anchor_idx) {
                    d_fs_drive_overrides++;
                    best_idx = d_fs_anchor_idx;
                    d_locked_idx = d_fs_anchor_idx;
                }
            }

            // ---- Lock state machine ----
            const bool was_locked = d_seg_locked;
            const bool fs_holds = (d_fs_hold_lock && d_fs_locked &&
                                   d_segs_since_fs <= d_fs_hold);
            if (d_seg_locked) {
                bool ok = (snr_ratio >= d_unlock_threshold);
                if (!ok && fs_holds) {
                    ok = true;
                    d_fs_held_locks++;
                }
                d_seg_locked = ok;
                if (!d_seg_locked) {
                    d_locked_idx = -1;
                }
            } else {
                bool acq = (snr_ratio >= d_lock_threshold);
                if (!acq && fs_holds && d_fs_anchor_idx >= 0) {
                    // FS lock implies segment alignment is known; force
                    // re-acquire here so the per-sample timing loop and
                    // bin sticky-search have something to track.
                    acq = true;
                    best_idx = d_fs_anchor_idx;
                }
                d_seg_locked = acq;
                if (d_seg_locked) {
                    d_locked_idx = best_idx;
                    if (!was_locked) d_relocks++;
                }
            }

            // ---- Timing-error gradient ----
            int corr_count = best_idx;
            d_timing_adjust = -d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = ATSC_DATA_SEGMENT_LENGTH - 1;
            d_timing_adjust -= d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = ATSC_DATA_SEGMENT_LENGTH - 1;
            d_timing_adjust += d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = ATSC_DATA_SEGMENT_LENGTH - 1;
            d_timing_adjust += d_sample_mem[corr_count--];

            d_symbol_index = SYMBOL_INDEX_OFFSET - 1 - best_idx;
            if (d_symbol_index < 0) d_symbol_index += ATSC_DATA_SEGMENT_LENGTH;
            d_counter = 0;

            d_seg_count++;
            if (d_debug && (d_seg_count % 256 == 0)) {
                std::fprintf(stderr,
                             "[sync_fieldlock] seg=%llu peak=%+.2f rms=%.3f "
                             "snr=%.2f locked=%d fslock=%d sslf=%d best=%d "
                             "fs_anchor=%d emitted=%llu aligned=%llu (%.2f%%)\n",
                             (unsigned long long)d_seg_count, best_val, rms,
                             snr_ratio, (int)d_seg_locked, (int)d_fs_locked,
                             d_segs_since_fs, best_idx, d_fs_anchor_idx,
                             (unsigned long long)d_segs_emitted,
                             (unsigned long long)d_segs_aligned,
                             d_segs_emitted > 0
                                ? 100.0 * (double)d_segs_aligned / (double)d_segs_emitted
                                : 0.0);
            }
            (void)was_locked;
        }

        const bool will_emit = d_seg_locked || d_emit_when_unlocked;
        if (will_emit) {
            d_data_mem[d_symbol_index] = interp_sample;
            if (d_symbol_index >= (ATSC_DATA_SEGMENT_LENGTH - 1)) {
                float* out_seg = &out[d_output_produced * ATSC_DATA_SEGMENT_LENGTH];
                memcpy(out_seg, d_data_mem,
                       ATSC_DATA_SEGMENT_LENGTH * sizeof(float));
                d_output_produced++;
                d_segs_emitted++;
                if (out_seg[0] > 0 && out_seg[1] < 0 &&
                    out_seg[2] < 0 && out_seg[3] > 0) {
                    d_segs_aligned++;
                }

                // ---- Field-Sync correlation on the just-emitted segment ----
                float fs_corr;
                int fs_offset;
                bool fs_hit = fs_check(out_seg, fs_corr, fs_offset);
                if (fs_hit) {
                    d_fs_detections++;
                    if (d_fs_correct && fs_offset != 0 && d_locked_idx >= 0) {
                        // Bin needs to slide by fs_offset samples. The
                        // d_symbol_index reset on next d_counter wrap is
                        // (2 - best_idx) mod 832, so shifting best_idx by
                        // +fs_offset advances d_symbol_index reset by
                        // -fs_offset, which means d_data_mem will start
                        // emitting fs_offset samples later — undoing the
                        // misalignment.
                        int new_idx = d_locked_idx + fs_offset;
                        if (new_idx < 0) new_idx += ATSC_DATA_SEGMENT_LENGTH;
                        if (new_idx >= ATSC_DATA_SEGMENT_LENGTH) new_idx -= ATSC_DATA_SEGMENT_LENGTH;
                        d_locked_idx = new_idx;
                        d_fs_corrections++;
                    }
                    if (d_locked_idx >= 0) {
                        d_fs_anchor_idx = d_locked_idx;
                        d_fs_locked = true;
                        d_segs_since_fs = 0;
                    }
                } else {
                    d_segs_since_fs++;
                    if (d_segs_since_fs > d_fs_hold) {
                        d_fs_locked = false;
                    }
                }
                if (d_debug && (d_segs_emitted % 256 == 0)) {
                    std::fprintf(stderr,
                                 "[sync_fieldlock fs] emit=%llu fs_corr=%+.1f "
                                 "fs_off=%+d hit=%d fslocked=%d sslf=%d\n",
                                 (unsigned long long)d_segs_emitted, fs_corr,
                                 fs_offset, (int)fs_hit, (int)d_fs_locked,
                                 d_segs_since_fs);
                }
            }
        } else {
            if (d_symbol_index >= (ATSC_DATA_SEGMENT_LENGTH - 1)) {
                d_segs_held++;
            }
        }
    }

    consume_each(d_si);
    return d_output_produced;
}

} /* namespace atscplus */
} /* namespace gr */
