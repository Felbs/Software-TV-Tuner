/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Path A: FS-anchored phase reset + locked-idx freeze under fades.
 *
 * Architecture: identical 1664-sample symbol ring + 832-tap sliding FS
 * correlator as atsc_sync_slidefs. The two structural deltas:
 *
 *   (a) On each FS detection, FORCE d_symbol_index and d_locked_idx to
 *       the FS-derived expected values. d_symbol_index_expected =
 *       (d_total_symbols - 1 - fs_anchor_symbol) % 832 — the absolute-time
 *       phase of the current sample, anchored to the FS first +1.
 *       This is the "reset at FS, run free between" model: we don't
 *       extrapolate per-iteration (which fights soft's per-segment relock),
 *       we just force a one-shot re-anchor at each FS detection.
 *
 *   (b) Between FSes, FREEZE d_locked_idx updates when matched-filter SNR
 *       drops below ATSC_SYNC_PA_FREEZE_SNR (default 3.0). During fades,
 *       the 4-tap MF argmax wanders by 1-2 bins; freezing it prevents
 *       1-2 sample mis-positioning of d_data_mem layout. The d_timing_adjust
 *       feedback is also frozen (so per-sample d_mu doesn't drift on
 *       noise-driven gradient).
 */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_sync_pathA_impl.h"
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

atsc_sync_pathA::sptr atsc_sync_pathA::make(float rate)
{
    return gnuradio::make_block_sptr<atsc_sync_pathA_impl>(rate);
}

atsc_sync_pathA_impl::atsc_sync_pathA_impl(float rate)
    : gr::block("atscplus_atsc_sync_pathA",
                io_signature::make(1, 1, sizeof(float)),
                io_signature::make(1, 1, ATSC_DATA_SEGMENT_LENGTH * sizeof(float))),
      d_rx_clock_to_symbol_freq(rate / ATSC_SYMBOL_RATE),
      d_si(0)
{
    d_loop.set_taps(LOOP_FILTER_TAP);

    d_alpha = 0.40f;
    d_lock_threshold = 4.0f;
    d_unlock_threshold = 2.0f;
    d_sticky_fraction = 0.95f;
    d_freeze_snr = 3.0f;
    d_timing_freeze = true;
    d_fs_reset_enabled = true;
    d_emit_when_unlocked = true;
    d_debug = false;
    d_timing_gain_scale = 1.0f;

    d_fs_threshold = 2500.0f;
    d_fs_check_period_locked = ATSC_DATA_SEGMENT_LENGTH;
    d_fs_check_period_bootstrap = ATSC_DATA_SEGMENT_LENGTH;
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

    getf("ATSC_SYNC_PA_ALPHA", 0.0f, 1.0f, d_alpha);
    getf("ATSC_SYNC_PA_LOCK", 0.0f, 100.0f, d_lock_threshold);
    getf("ATSC_SYNC_PA_UNLOCK", 0.0f, 100.0f, d_unlock_threshold);
    getf("ATSC_SYNC_PA_STICKY", 0.0f, 1.0f, d_sticky_fraction);
    getf("ATSC_SYNC_PA_FREEZE_SNR", 0.0f, 100.0f, d_freeze_snr);
    getf("ATSC_SYNC_PA_TIMING_SCALE", 0.0f, 10.0f, d_timing_gain_scale);
    getf("ATSC_SYNC_PA_FS_THR", 0.0f, 1e8f, d_fs_threshold);
    geti("ATSC_SYNC_PA_FS_PERIOD", 1, 1<<20, d_fs_check_period_locked);
    geti("ATSC_SYNC_PA_FS_BOOTSTRAP", 1, 1<<20, d_fs_check_period_bootstrap);
    geti("ATSC_SYNC_PA_FS_HOLD", 0, 1<<20, d_fs_hold);
    getb("ATSC_SYNC_PA_TIMING_FREEZE", d_timing_freeze);
    getb("ATSC_SYNC_PA_FS_RESET", d_fs_reset_enabled);
    getb("ATSC_SYNC_PA_EMIT_UNLOCKED", d_emit_when_unlocked);
    getb("ATSC_SYNC_PA_DEBUG", d_debug);

    d_segs_emitted = 0;
    d_segs_aligned = 0;
    d_relocks = 0;
    d_seg_count = 0;
    d_fs_detections = 0;
    d_fs_resets = 0;
    d_locked_idx_freezes = 0;

    build_fs_template();

    std::fprintf(stderr,
        "[sync_pathA] rate=%.0f alpha=%.4f lock=%.2f unlock=%.2f sticky=%.2f "
        "freeze_snr=%.2f timing_scale=%.2f timing_freeze=%d fs_reset=%d "
        "fs_thr=%.2f fs_period=%d fs_boot=%d fs_hold=%d fs_active=%zu "
        "emit_unlocked=%d debug=%d ring_len=%d\n",
        rate, d_alpha, d_lock_threshold, d_unlock_threshold, d_sticky_fraction,
        d_freeze_snr, d_timing_gain_scale, (int)d_timing_freeze,
        (int)d_fs_reset_enabled, d_fs_threshold,
        d_fs_check_period_locked, d_fs_check_period_bootstrap, d_fs_hold,
        d_fs_active_idx.size(), (int)d_emit_when_unlocked, (int)d_debug, RING_LEN);

    reset();
}

void atsc_sync_pathA_impl::build_fs_template()
{
    for (int i = 0; i < FS_TPL_LEN; i++) d_fs_template[i] = 0.0f;

    d_fs_template[0] = +1.0f;
    d_fs_template[1] = -1.0f;
    d_fs_template[2] = -1.0f;
    d_fs_template[3] = +1.0f;

    for (int k = 0; k < 511; k++)
        d_fs_template[4 + k] = (atsc_pn511[k] != 0) ? +1.0f : -1.0f;
    for (int k = 0; k < 63; k++)
        d_fs_template[515 + k] = (atsc_pn63[k] != 0) ? +1.0f : -1.0f;
    for (int k = 0; k < 63; k++)
        d_fs_template[641 + k] = (atsc_pn63[k] != 0) ? +1.0f : -1.0f;

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

void atsc_sync_pathA_impl::reset()
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
    memset(d_ring, 0, sizeof(d_ring));
    d_total_symbols = 0;

    d_fs_locked = false;
    d_fs_anchor_symbol = 0;
    d_fs_segs_since = 0;
    d_symbols_until_check = d_fs_check_period_bootstrap;
}

atsc_sync_pathA_impl::~atsc_sync_pathA_impl()
{
    double align_pct = (d_segs_emitted > 0)
        ? 100.0 * (double)d_segs_aligned / (double)d_segs_emitted : 0.0;
    std::fprintf(stderr,
        "[sync_pathA FINAL] segs_emitted=%llu segs_aligned=%llu (%.2f%%) "
        "relocks=%llu fs_detections=%llu fs_resets=%llu "
        "locked_idx_freezes=%llu total_symbols=%llu\n",
        (unsigned long long)d_segs_emitted,
        (unsigned long long)d_segs_aligned, align_pct,
        (unsigned long long)d_relocks,
        (unsigned long long)d_fs_detections,
        (unsigned long long)d_fs_resets,
        (unsigned long long)d_locked_idx_freezes,
        (unsigned long long)d_total_symbols);
}

void atsc_sync_pathA_impl::forecast(int noutput_items,
                                    gr_vector_int& ninput_items_required)
{
    unsigned ninputs = ninput_items_required.size();
    for (unsigned i = 0; i < ninputs; i++)
        ninput_items_required[i] =
            static_cast<int>(noutput_items * d_rx_clock_to_symbol_freq *
                             ATSC_DATA_SEGMENT_LENGTH) + 1500 - 1;
}

float atsc_sync_pathA_impl::fs_slide_search(int& ring_off,
                                            int range_lo, int range_hi)
{
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

int atsc_sync_pathA_impl::general_work(int noutput_items,
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

        // Per-sample timing-adjust drives MMSE interpolator phase.
        // Path A change: when 4-tap MF SNR is low (we'll detect this at
        // the segment-cycle wrap below), d_timing_adjust is the LAST
        // computed value (frozen). Per-sample d_mu still gets pushed by
        // the frozen value, but it's the last-known-good gradient, not
        // fresh noise. For a stronger freeze, d_timing_adjust is set to
        // 0 below when freezing — see the freeze-block logic.
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

        // ---- Soft-base bookkeeping ----
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
            float peak;
            int   ring_off;
            peak = fs_slide_search(ring_off, 0, RING_LEN - FS_TPL_LEN);

            if (ring_off >= 0 && peak > d_fs_threshold) {
                fs_hit_now = true;
                d_fs_detections++;
                const uint64_t base = d_total_symbols - (uint64_t)RING_LEN;
                uint64_t fs_start = base + (uint64_t)ring_off;
                d_fs_anchor_symbol = fs_start;
                d_fs_segs_since = 0;
                if (!d_fs_locked) {
                    d_fs_locked = true;
                    if (d_debug) {
                        std::fprintf(stderr,
                            "[sync_pathA] FS LOCK fs_start=%llu peak=%.1f "
                            "off=%d total=%llu\n",
                            (unsigned long long)fs_start, peak, ring_off,
                            (unsigned long long)d_total_symbols);
                    }
                } else if (d_debug) {
                    std::fprintf(stderr,
                        "[sync_pathA] FS det fs_start=%llu peak=%.1f off=%d\n",
                        (unsigned long long)fs_start, peak, ring_off);
                }
            }

            d_symbols_until_check = d_fs_locked
                ? d_fs_check_period_locked
                : d_fs_check_period_bootstrap;
        }

        // ---- Per-cycle MF aggregation ----
        d_counter++;
        bool cycle_wrap = false;
        int  cycle_best_idx = -1;
        bool cycle_freeze = false;
        double cycle_snr_ratio = 0.0;
        float  cycle_best_val = 0.0f;
        if (d_counter >= FS_TPL_LEN) {
            cycle_wrap = true;
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

            cycle_snr_ratio = snr_ratio;
            cycle_best_val = best_val;

            // Path A: FREEZE policy.
            // If we have a previous d_locked_idx (locked at some prior good
            // cycle) AND current snr_ratio < d_freeze_snr, hold d_locked_idx
            // unchanged. The 4-tap argmax during fades is noise-driven and
            // typically wanders ±1-2 bins; freezing prevents that wander
            // from corrupting d_data_mem layout.
            const bool have_lock = (d_locked_idx >= 0 &&
                                    d_locked_idx < FS_TPL_LEN);
            cycle_freeze = (have_lock && snr_ratio < d_freeze_snr);

            if (cycle_freeze) {
                d_locked_idx_freezes++;
                best_idx = d_locked_idx;
                // Don't update lock state — hold previous d_seg_locked.
            } else {
                // Sticky-lock mini-search around d_locked_idx (mirrors soft).
                if (d_seg_locked && have_lock) {
                    static constexpr int SEARCH_W = 6;
                    int local_best_idx = d_locked_idx;
                    float local_best_val = d_integrator[d_locked_idx];
                    for (int dd = -SEARCH_W; dd <= SEARCH_W; dd++) {
                        int j = d_locked_idx + dd;
                        if (j < 0) j += FS_TPL_LEN;
                        if (j >= FS_TPL_LEN) j -= FS_TPL_LEN;
                        if (d_integrator[j] > local_best_val) {
                            local_best_val = d_integrator[j];
                            local_best_idx = j;
                        }
                    }
                    const float locked_val = d_integrator[d_locked_idx];
                    if (locked_val >= d_sticky_fraction * best_val) {
                        best_idx = local_best_idx;
                        d_locked_idx = local_best_idx;
                    } else {
                        d_locked_idx = best_idx;
                        d_relocks++;
                    }
                }

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
            }

            cycle_best_idx = best_idx;

            // Update d_timing_adjust gradient.
            // Path A: when frozen, set d_timing_adjust = 0 (don't push d_mu
            // on noise-driven gradient). When not frozen, use standard
            // 4-tap discrete-derivative formula.
            if (cycle_freeze && d_timing_freeze) {
                d_timing_adjust = 0.0;
            } else {
                int corr_count = best_idx;
                d_timing_adjust = -d_sample_mem[corr_count--];
                if (corr_count < 0) corr_count = FS_TPL_LEN - 1;
                d_timing_adjust -= d_sample_mem[corr_count--];
                if (corr_count < 0) corr_count = FS_TPL_LEN - 1;
                d_timing_adjust += d_sample_mem[corr_count--];
                if (corr_count < 0) corr_count = FS_TPL_LEN - 1;
                d_timing_adjust += d_sample_mem[corr_count--];
            }

            d_counter = 0;
            d_seg_count++;
        }

        // ---- Drive d_symbol_index ----
        // Path A: when fs_locked AND a recent FS, suppress soft's
        // per-cycle d_symbol_index reset. The FS reset (below) provides
        // an absolute-time anchor that soft's noise-driven best_idx-reset
        // would otherwise stomp on within ~half a cycle.
        const bool fs_drive = d_fs_reset_enabled && d_fs_locked &&
                              d_fs_segs_since < d_fs_hold;
        if (cycle_wrap && !fs_drive) {
            d_symbol_index = SYMBOL_INDEX_OFFSET - 1 - cycle_best_idx;
            if (d_symbol_index < 0) d_symbol_index += FS_TPL_LEN;
        } else {
            d_symbol_index++;
            if (d_symbol_index >= FS_TPL_LEN) d_symbol_index = 0;
        }

        // Path A: FS-driven d_symbol_index reset at the moment of FS detection.
        // The current sample's absolute-time position is (d_total_symbols - 1).
        // It should map to d_data_mem position = (abs_idx - fs_anchor) mod 832.
        // The CHAIN RATE may drift between FSes (~0.7%), so the FS reset gives
        // a fresh absolute reference once per ~313 segments. Between FSes,
        // soft's per-segment local search tracks the slow drift.
        if (fs_hit_now && d_fs_reset_enabled) {
            int64_t expected = (int64_t)(d_total_symbols - 1)
                             - (int64_t)d_fs_anchor_symbol;
            int64_t expected_mod = expected % (int64_t)FS_TPL_LEN;
            if (expected_mod < 0) expected_mod += FS_TPL_LEN;
            int new_symbol_index = (int)expected_mod;

            // Also reset d_locked_idx so that the next cycle wrap's
            // sticky-search anchors near the FS-derived position.
            // Mapping: at cycle wrap, d_symbol_index = OFFSET - 1 - best_idx.
            // Inverse: best_idx = OFFSET - 1 - d_symbol_index (mod 832).
            // We're MID-cycle here; project forward to the next cycle's
            // best_idx by computing what best_idx WOULD be at the next wrap
            // if d_symbol_index continues to increment 1/iter from current.
            // At next cycle wrap, d_counter = 832 → d_total_symbols increases
            // by (832 - d_counter) more, so d_symbol_index_at_wrap =
            // (new_symbol_index + (832 - d_counter)) mod 832.
            int seg_idx_at_wrap =
                (new_symbol_index + (FS_TPL_LEN - d_counter)) % FS_TPL_LEN;
            int new_locked_idx = SYMBOL_INDEX_OFFSET - 1 - seg_idx_at_wrap;
            if (new_locked_idx < 0) new_locked_idx += FS_TPL_LEN;
            new_locked_idx = (FS_TPL_LEN - new_locked_idx) % FS_TPL_LEN;
            // Simpler & equivalent: use new_locked_idx = (cycle pos at wrap
            // where +1 of segsync first appears) — but the existing best_idx
            // semantics is the bin where MF peaks (= position of LAST +1, ie
            // first +1 + 3). So expected_best_idx_at_wrap = (idx of FIRST +1
            // mod 832) + 3 (if the first +1 falls in the next cycle). We
            // approximate by setting d_locked_idx to the FS-derived value
            // and let sticky-search fine-tune.
            (void)new_locked_idx;

            d_symbol_index = new_symbol_index;
            d_fs_resets++;
            // Force d_locked_idx based on FS-derived d_symbol_index.
            // d_locked_idx is the bin within d_counter cycle. At the moment
            // we're at d_counter (post-increment, just took this sample),
            // the LAST sample of d_sample_mem at index d_counter-1 (or
            // d_counter wrapped). The FS template's first +1 corresponds to
            // d_data_mem[0]; the MF peak (last +1) corresponds to d_data_mem[3].
            // Per-cycle d_locked_idx should index into d_sample_mem such
            // that d_sample_mem[d_locked_idx] holds the LAST +1 of segsync.
            // Equivalently, d_locked_idx = (d_counter - (d_symbol_index - 3)) mod 832.
            // Where d_symbol_index = position in d_data_mem of CURRENT sample.
            // d_locked_idx = (d_counter - d_symbol_index + SYMBOL_INDEX_OFFSET) mod 832.
            int candidate = ((d_counter - 1) - d_symbol_index + SYMBOL_INDEX_OFFSET);
            candidate %= FS_TPL_LEN;
            if (candidate < 0) candidate += FS_TPL_LEN;
            d_locked_idx = candidate;
            d_seg_locked = true;
        }

        // ---- Drop FS lock if no detection in too long ----
        if (d_fs_locked && d_fs_segs_since > d_fs_hold) {
            if (d_debug) {
                std::fprintf(stderr,
                    "[sync_pathA] FS lock dropped (segs_since=%d > %d)\n",
                    d_fs_segs_since, d_fs_hold);
            }
            d_fs_locked = false;
            d_symbols_until_check = d_fs_check_period_bootstrap;
        }

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
                if (d_debug && (d_segs_emitted % 1024 == 0)) {
                    double pct = d_segs_emitted > 0
                        ? 100.0 * d_segs_aligned / d_segs_emitted : 0.0;
                    std::fprintf(stderr,
                        "[sync_pathA] emit=%llu aligned=%llu (%.2f%%) "
                        "fs_locked=%d fs_det=%llu fs_resets=%llu freezes=%llu "
                        "snr_ratio=%.2f best_val=%.1f\n",
                        (unsigned long long)d_segs_emitted,
                        (unsigned long long)d_segs_aligned, pct,
                        (int)d_fs_locked,
                        (unsigned long long)d_fs_detections,
                        (unsigned long long)d_fs_resets,
                        (unsigned long long)d_locked_idx_freezes,
                        cycle_snr_ratio, cycle_best_val);
                }
            }
        }
        (void)cycle_freeze;
    }

    consume_each(d_si);
    return d_output_produced;
}

} /* namespace atscplus */
} /* namespace gr */
