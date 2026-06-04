/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Sliding-Field-Sync ATSC segment-sync block (Option 1.5).
 *
 * Why this exists: atsc_sync_fieldlock plateaus at 91% segment alignment
 * because its FS correlation runs against d_data_mem (the per-segment-cycle
 * buffer), and when the 4-tap detector mis-locks by >3 samples, d_data_mem
 * holds only (832-k) symbols of any FS that overlaps it — a circular shift
 * cannot recover the missing k symbols. See ~/overnight/FIELD_SYNC_RESULT.md
 * for the diagnosis and the W=416 negative result that established this.
 *
 * The fix is architectural: maintain a 1664-sample ring buffer of EVERY
 * interpolated symbol, decoupled from segment-cycle state. Slide-correlate
 * the 832-tap FS template across the ring (linear, not cyclic — the 2x size
 * guarantees full FS coverage at any phase). On detection, anchor the
 * segment cycle to absolute symbol-time. Subsequent segment boundaries are
 * deterministic at FS_anchor + 832k.
 */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_sync_slidefs_impl.h"
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

atsc_sync_slidefs::sptr atsc_sync_slidefs::make(float rate)
{
    return gnuradio::make_block_sptr<atsc_sync_slidefs_impl>(rate);
}

atsc_sync_slidefs_impl::atsc_sync_slidefs_impl(float rate)
    : gr::block("atscplus_atsc_sync_slidefs",
                io_signature::make(1, 1, sizeof(float)),
                io_signature::make(1, 1, ATSC_DATA_SEGMENT_LENGTH * sizeof(float))),
      d_rx_clock_to_symbol_freq(rate / ATSC_SYMBOL_RATE),
      d_si(0)
{
    d_loop.set_taps(LOOP_FILTER_TAP);

    // Soft-base defaults — same family as soft / fieldlock.
    d_alpha = 0.40f;
    d_lock_threshold = 4.0f;
    d_unlock_threshold = 2.0f;
    d_emit_when_unlocked = true;
    d_debug = false;
    d_timing_gain_scale = 1.0f;

    // Sliding-FS defaults.
    // - Threshold 2500 cleanly separates real FS (~3200) from noise (<2100).
    // - Period 832 (one check per segment) keeps cost <5% of replay budget.
    //   Bootstrap matches because we only emit segments at >=1 segment cadence.
    // - Drift window 4 = expected ±0.4 sample drift per segment at 100ppm clock,
    //   reasonable for a single-segment search after lock.
    // - Hold 626 = 2 fields, bridges single missed FS.
    d_fs_threshold = 2500.0f;
    d_fs_check_period_locked = ATSC_DATA_SEGMENT_LENGTH;
    d_fs_check_period_bootstrap = ATSC_DATA_SEGMENT_LENGTH;
    // Drift window is ONLY used to classify drift vs. jump for stats; full
    // search now finds FS regardless of where it sits in the ring.
    // 50 samples = ~192 ppm clock drift between FS observations (313 segs).
    d_fs_drift_w = 50;
    d_fs_hold = 626;

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

    getf("ATSC_SYNC_SF_ALPHA", 0.0f, 1.0f, d_alpha);
    getf("ATSC_SYNC_SF_LOCK", 0.0f, 100.0f, d_lock_threshold);
    getf("ATSC_SYNC_SF_UNLOCK", 0.0f, 100.0f, d_unlock_threshold);
    getf("ATSC_SYNC_SF_TIMING_SCALE", 0.0f, 10.0f, d_timing_gain_scale);
    getf("ATSC_SYNC_SF_FS_THR", 0.0f, 1e8f, d_fs_threshold);
    geti("ATSC_SYNC_SF_FS_PERIOD", 1, 1<<20, d_fs_check_period_locked);
    geti("ATSC_SYNC_SF_FS_BOOTSTRAP", 1, 1<<20, d_fs_check_period_bootstrap);
    geti("ATSC_SYNC_SF_FS_HOLD", 0, 1<<20, d_fs_hold);
    geti("ATSC_SYNC_SF_FS_DRIFT_W", 0, 416, d_fs_drift_w);
    getb("ATSC_SYNC_SF_EMIT_UNLOCKED", d_emit_when_unlocked);
    getb("ATSC_SYNC_SF_DEBUG", d_debug);

    d_segs_emitted = 0;
    d_segs_aligned = 0;
    d_relocks = 0;
    d_seg_count = 0;
    d_fs_detections = 0;
    d_fs_anchor_corrections = 0;
    d_fs_anchor_drifts = 0;
    d_fs_anchor_jumps = 0;

    build_fs_template();

    std::fprintf(stderr,
                 "[sync_slidefs] rate=%.0f alpha=%.4f lock=%.2f unlock=%.2f "
                 "timing_scale=%.2f fs_thr=%.2f fs_period=%d fs_boot=%d "
                 "fs_hold=%d fs_drift_w=%d fs_active=%zu emit_unlocked=%d "
                 "debug=%d ring_len=%d\n",
                 rate, d_alpha, d_lock_threshold, d_unlock_threshold,
                 d_timing_gain_scale, d_fs_threshold,
                 d_fs_check_period_locked, d_fs_check_period_bootstrap,
                 d_fs_hold, d_fs_drift_w, d_fs_active_idx.size(),
                 (int)d_emit_when_unlocked, (int)d_debug, RING_LEN);

    reset();
}

void atsc_sync_slidefs_impl::build_fs_template()
{
    for (int i = 0; i < FS_TPL_LEN; i++) d_fs_template[i] = 0.0f;

    // Segment-sync 4-tap (always present).
    d_fs_template[0] = +1.0f;
    d_fs_template[1] = -1.0f;
    d_fs_template[2] = -1.0f;
    d_fs_template[3] = +1.0f;

    // PN511 at positions 4..514.
    for (int k = 0; k < 511; k++) {
        d_fs_template[4 + k] = (atsc_pn511[k] != 0) ? +1.0f : -1.0f;
    }
    // First PN63 at 515..577.
    for (int k = 0; k < 63; k++) {
        d_fs_template[515 + k] = (atsc_pn63[k] != 0) ? +1.0f : -1.0f;
    }
    // Middle PN63 (578..640): zeroed (sign-flips between Field 1 and 2).
    // Third PN63 (641..703): same in both fields.
    for (int k = 0; k < 63; k++) {
        d_fs_template[641 + k] = (atsc_pn63[k] != 0) ? +1.0f : -1.0f;
    }
    // VSB mode bits (704..727), reserved+precode (728..831): zeroed.

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

void atsc_sync_slidefs_impl::reset()
{
    d_w = d_rx_clock_to_symbol_freq;
    // Manual d_w override — for Path A validation (chain rate mismatch).
    // On sdr_RF36.cf32 the chain produces output at ~10.39 MHz instead of
    // ATSC's 10.76 MHz; setting d_w = 1.5 * 251264/260416 = 1.4474 should
    // normalize the rate. Set ATSC_SYNC_SF_W_OVERRIDE to test.
    if (const char* p = std::getenv("ATSC_SYNC_SF_W_OVERRIDE")) {
        double v = std::atof(p);
        if (v > 0.5 && v < 5.0) {
            std::fprintf(stderr,
                "[sync_slidefs] d_w override: %.6f -> %.6f\n",
                d_rx_clock_to_symbol_freq, v);
            d_w = v;
        }
    }
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
    memset(d_ring, 0, sizeof(d_ring));
    d_total_symbols = 0;

    d_fs_locked = false;
    d_fs_anchor_symbol = 0;
    d_fs_segs_since = 0;
    d_symbols_until_check = d_fs_check_period_bootstrap;
}

atsc_sync_slidefs_impl::~atsc_sync_slidefs_impl()
{
    double align_pct = (d_segs_emitted > 0)
        ? 100.0 * (double)d_segs_aligned / (double)d_segs_emitted : 0.0;
    std::fprintf(stderr,
                 "[sync_slidefs FINAL] segs_emitted=%llu segs_aligned=%llu "
                 "(%.2f%%) relocks=%llu fs_detections=%llu fs_drifts=%llu "
                 "fs_jumps=%llu fs_corrections=%llu total_symbols=%llu\n",
                 (unsigned long long)d_segs_emitted,
                 (unsigned long long)d_segs_aligned, align_pct,
                 (unsigned long long)d_relocks,
                 (unsigned long long)d_fs_detections,
                 (unsigned long long)d_fs_anchor_drifts,
                 (unsigned long long)d_fs_anchor_jumps,
                 (unsigned long long)d_fs_anchor_corrections,
                 (unsigned long long)d_total_symbols);
}

void atsc_sync_slidefs_impl::forecast(int noutput_items,
                                      gr_vector_int& ninput_items_required)
{
    unsigned ninputs = ninput_items_required.size();
    for (unsigned i = 0; i < ninputs; i++)
        ninput_items_required[i] =
            static_cast<int>(noutput_items * d_rx_clock_to_symbol_freq *
                             ATSC_DATA_SEGMENT_LENGTH) + 1500 - 1;
}

float atsc_sync_slidefs_impl::fs_slide_search(int& ring_off,
                                              int range_lo, int range_hi)
{
    // Linear (non-cyclic) sliding correlation across d_ring.
    // The ring's oldest symbol (number d_total_symbols - RING_LEN) is at ring
    // index (d_total_symbols - RING_LEN) % RING_LEN. We search shift s so that
    // tpl[0] aligns with that-symbol-plus-s. Range [range_lo, range_hi] limits
    // search to a slice of [0, RING_LEN-FS_TPL_LEN].
    if (d_total_symbols < (uint64_t)RING_LEN) {
        ring_off = -1;
        return 0.0f;
    }
    const int N = (int)d_fs_active_idx.size();
    const int* ai = d_fs_active_idx.data();
    const float* as = d_fs_active_sign.data();

    const uint64_t base = d_total_symbols - (uint64_t)RING_LEN;

    int lo = std::max(0, range_lo);
    int hi = std::min(RING_LEN - FS_TPL_LEN, range_hi);

    float best = -1e30f;
    int best_off = lo;
    for (int s = lo; s <= hi; s++) {
        const uint64_t start = base + (uint64_t)s;
        float corr = 0.0f;
        for (int j = 0; j < N; j++) {
            int idx = (int)((start + (uint64_t)ai[j]) % (uint64_t)RING_LEN);
            corr += as[j] * d_ring[idx];
        }
        if (corr > best) { best = corr; best_off = s; }
    }
    ring_off = best_off;
    return best;
}

int atsc_sync_slidefs_impl::general_work(int noutput_items,
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

        // ---- Continuous symbol ring (decoupled from segment cycle) ----
        d_ring[d_total_symbols % (uint64_t)RING_LEN] = interp_sample;
        d_total_symbols++;

        // ---- Soft-base bookkeeping (4-tap MF + EMA integrator) ----
        d_sample_mem[d_counter] = interp_sample;

        d_mf_buf[d_mf_idx] = interp_sample;
        d_mf_idx = (d_mf_idx + 1) & 3;
        const float x_nm3 = d_mf_buf[d_mf_idx];
        const float x_nm2 = d_mf_buf[(d_mf_idx + 1) & 3];
        const float x_nm1 = d_mf_buf[(d_mf_idx + 2) & 3];
        const float x_n   = d_mf_buf[(d_mf_idx + 3) & 3];
        const float corr_4t = x_nm3 - x_nm2 - x_nm1 + x_n;
        d_integrator[d_counter] =
            (1.0f - d_alpha) * d_integrator[d_counter] + d_alpha * corr_4t;

        // ---- Sliding FS check ----
        if (d_symbols_until_check > 0) d_symbols_until_check--;
        bool fs_hit_now = false;
        if (d_symbols_until_check == 0 && d_total_symbols >= (uint64_t)RING_LEN) {
            // Always full search over [0, RING_LEN-832]. With ring=1664 and
            // FS=832, this is at most 832 shifts. Narrow search around
            // expected anchor position would be cheaper but breaks once the
            // observed-FS-to-expected drift (~26 samples per FS interval at
            // 100ppm clock) exceeds the search half-width. Full search is
            // robust and cheap enough at one check per segment (~7 GFLOPS).
            float peak;
            int   ring_off;
            peak = fs_slide_search(ring_off, 0, RING_LEN - FS_TPL_LEN);

            if (ring_off >= 0 && peak > d_fs_threshold) {
                fs_hit_now = true;
                d_fs_detections++;
                const uint64_t base = d_total_symbols - (uint64_t)RING_LEN;
                uint64_t fs_start = base + (uint64_t)ring_off;
                if (!d_fs_locked) {
                    d_fs_anchor_symbol = fs_start;
                    d_fs_locked = true;
                    d_fs_anchor_jumps++;
                    if (d_debug) {
                        std::fprintf(stderr,
                            "[sync_slidefs] LOCK fs_start=%llu peak=%.1f off=%d "
                            "total=%llu\n",
                            (unsigned long long)fs_start, peak, ring_off,
                            (unsigned long long)d_total_symbols);
                    }
                } else {
                    int64_t delta = (int64_t)fs_start - (int64_t)d_fs_anchor_symbol;
                    int64_t mod = delta % (int64_t)FS_TPL_LEN;
                    if (mod < 0) mod += FS_TPL_LEN;
                    if (mod > FS_TPL_LEN / 2) mod -= FS_TPL_LEN;
                    int drift = (int)mod;
                    if (d_debug) {
                        std::fprintf(stderr,
                            "[sync_slidefs] FS det fs_start=%llu peak=%.1f "
                            "off=%d delta=%lld drift=%d\n",
                            (unsigned long long)fs_start, peak, ring_off,
                            (long long)delta, drift);
                    }
                    if (std::abs(drift) <= d_fs_drift_w) {
                        if (drift != 0) {
                            d_fs_anchor_symbol = fs_start;
                            d_fs_anchor_drifts++;
                            d_fs_anchor_corrections++;
                        } else {
                            d_fs_anchor_symbol = fs_start;
                        }
                    } else {
                        d_fs_anchor_symbol = fs_start;
                        d_fs_anchor_jumps++;
                        d_fs_anchor_corrections++;
                        d_relocks++;
                    }
                }
                d_fs_segs_since = 0;
            }

            d_symbols_until_check = d_fs_locked
                ? d_fs_check_period_locked
                : d_fs_check_period_bootstrap;
        }

        // ---- Run soft-block timing loop in parallel (always) ----
        // d_counter / d_integrator / d_timing_adjust are computed identically
        // to atsc_sync_soft, INDEPENDENT of FS lock state. This keeps the
        // MMSE interpolator's timing-recovery loop converged on the same
        // signal-derived gradient that's known to work, decoupled from the
        // FS-anchor jumps that would otherwise corrupt d_data_mem-based
        // gradient estimation.
        //
        // d_symbol_index is *separately* driven below: from FS anchor when
        // FS-locked, or from soft's best_idx-derived value otherwise.
        d_counter++;
        int soft_best_idx = -1;
        if (d_counter >= FS_TPL_LEN) {
            float best_val = d_integrator[0];
            int best_idx = 0;
            double sum_sq = 0.0;
            for (int i = 0; i < FS_TPL_LEN; i++) {
                const float v = d_integrator[i];
                sum_sq += (double)v * (double)v;
                if (v > best_val) { best_val = v; best_idx = i; }
            }
            const double rms = std::sqrt(sum_sq / (double)FS_TPL_LEN);
            const double snr_ratio = (rms > 1e-9) ? (double)best_val / rms : 0.0;

            if (d_seg_locked) {
                if (snr_ratio < d_unlock_threshold) {
                    d_seg_locked = false;
                    d_locked_idx = -1;
                }
            } else {
                if (snr_ratio >= d_lock_threshold) {
                    d_seg_locked = true;
                    d_locked_idx = best_idx;
                    d_relocks++;
                }
            }

            int corr_count = best_idx;
            d_timing_adjust = -d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = FS_TPL_LEN - 1;
            d_timing_adjust -= d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = FS_TPL_LEN - 1;
            d_timing_adjust += d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = FS_TPL_LEN - 1;
            d_timing_adjust += d_sample_mem[corr_count--];

            soft_best_idx = best_idx;
            d_counter = 0;
            d_seg_count++;
        }

        // ---- Drive d_symbol_index (soft semantics) ----
        // Mirror atsc_sync_soft EXACTLY: increment d_symbol_index per
        // iteration, reset to (2-best_idx) at d_counter wrap.
        //
        // The continuous-ring FS detector still runs above — its hits update
        // d_fs_anchor_symbol and d_fs_locked, but d_symbol_index is NOT
        // currently driven from those (see ARCH_FIX_RESULT.md for why and
        // what the next-session fix is).
        if (soft_best_idx >= 0) {
            d_symbol_index = SYMBOL_INDEX_OFFSET - 1 - soft_best_idx;
            if (d_symbol_index < 0) d_symbol_index += FS_TPL_LEN;
        } else {
            d_symbol_index++;
            if (d_symbol_index >= FS_TPL_LEN) d_symbol_index = 0;
        }
        (void)d_fs_locked; // FS detection runs but doesn't drive emit yet

        // ---- Emit segment when d_symbol_index hits 831 ----
        const bool will_emit = d_seg_locked || d_emit_when_unlocked;
        if (will_emit) {
            d_data_mem[d_symbol_index] = interp_sample;
            if (d_symbol_index >= (FS_TPL_LEN - 1)) {
                float* out_seg = &out[d_output_produced * FS_TPL_LEN];
                memcpy(out_seg, d_data_mem, FS_TPL_LEN * sizeof(float));
                d_output_produced++;
                d_segs_emitted++;
                if (out_seg[0] > 0 && out_seg[1] < 0 &&
                    out_seg[2] < 0 && out_seg[3] > 0) {
                    d_segs_aligned++;
                }
                d_fs_segs_since++;
                if (d_fs_locked && d_fs_segs_since > d_fs_hold) {
                    if (d_debug) {
                        std::fprintf(stderr,
                            "[sync_slidefs] FS lock dropped (segs_since=%d > %d)\n",
                            d_fs_segs_since, d_fs_hold);
                    }
                    d_fs_locked = false;
                    d_symbols_until_check = d_fs_check_period_bootstrap;
                }
                if (d_debug && (d_segs_emitted % 1024 == 0)) {
                    double pct = d_segs_emitted > 0
                        ? 100.0 * d_segs_aligned / d_segs_emitted : 0.0;
                    std::fprintf(stderr,
                        "[sync_slidefs] emit=%llu aligned=%llu (%.2f%%) "
                        "fs_locked=%d fs_det=%llu fs_drift=%llu fs_jump=%llu\n",
                        (unsigned long long)d_segs_emitted,
                        (unsigned long long)d_segs_aligned, pct,
                        (int)d_fs_locked,
                        (unsigned long long)d_fs_detections,
                        (unsigned long long)d_fs_anchor_drifts,
                        (unsigned long long)d_fs_anchor_jumps);
                }
            }
        }
        (void)fs_hit_now;
    }

    consume_each(d_si);
    return d_output_produced;
}

} /* namespace atscplus */
} /* namespace gr */
