/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Kalman-filter timing-recovery ATSC segment-sync detector.
 *
 * Inherits the soft matched-filter detector and EMA integrator from
 * atsc_sync_soft, then adds a 2-state Kalman filter (phase, rate) on
 * the per-segment timing-error gradient to suppress the noise that
 * gets integrated 832x/segment in atsc_sync_soft and limits its
 * alignment to ~91% on weak captures. See ~/overnight/SYNC_BUILD_RESULT.md
 * for the diagnosis that motivated this block.
 */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_sync_kalman_impl.h"
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

atsc_sync_kalman::sptr atsc_sync_kalman::make(float rate)
{
    return gnuradio::make_block_sptr<atsc_sync_kalman_impl>(rate);
}

atsc_sync_kalman_impl::atsc_sync_kalman_impl(float rate)
    : gr::block("atscplus_atsc_sync_kalman",
                io_signature::make(1, 1, sizeof(float)),
                io_signature::make(1, 1, ATSC_DATA_SEGMENT_LENGTH * sizeof(float))),
      d_rx_clock_to_symbol_freq(rate / ATSC_SYMBOL_RATE),
      d_si(0)
{
    d_loop.set_taps(LOOP_FILTER_TAP);

    // Soft-detector defaults: same as atsc_sync_soft (already tuned on RF36).
    d_alpha = 0.40f;
    d_lock_threshold = 4.0f;
    d_unlock_threshold = 2.0f;
    d_sticky_fraction = 0.95f;
    d_emit_when_unlocked = true;
    d_debug = false;
    d_locked_idx = -1;
    d_timing_gain_scale = 1.0f;
    d_local_move_factor = 1.10f;

    // Kalman defaults. Phase noise is small (process drifts slowly, ~0.05
    // gradient-units per segment); rate noise is much smaller (clock rate
    // changes very slowly). R_base is the variance of d_timing_adjust noise
    // observed at lock — measured around 5 in earlier diagnostics.
    d_Q_phase = 0.05;
    d_Q_rate  = 5.0e-4;
    d_R_base  = 5.0;
    d_init_P  = 100.0;
    d_kalman_bypass = false;

    d_snr_ema = 0.0;
    d_snr_ema_alpha = 0.30;     // EMA on snr_ratio (~3 segs effective window)
    d_low_snr_streak = 0;
    d_unlock_streak_req = 3;    // need 3 consecutive low-SNR segs to unlock

    if (const char* p = std::getenv("ATSC_SYNC_KALMAN_ALPHA")) {
        float v = std::atof(p);
        if (v > 0.0f && v <= 1.0f) d_alpha = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_KALMAN_LOCK")) {
        float v = std::atof(p);
        if (v > 0.0f) d_lock_threshold = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_KALMAN_UNLOCK")) {
        float v = std::atof(p);
        if (v > 0.0f) d_unlock_threshold = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_KALMAN_STICKY")) {
        float v = std::atof(p);
        if (v >= 0.0f && v <= 1.0f) d_sticky_fraction = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_KALMAN_TIMING_SCALE")) {
        float v = std::atof(p);
        if (v >= 0.0f && v <= 10.0f) d_timing_gain_scale = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_KALMAN_LOCAL_MOVE")) {
        float v = std::atof(p);
        if (v >= 1.0f && v <= 10.0f) d_local_move_factor = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_KALMAN_Q_PHASE")) {
        double v = std::atof(p);
        if (v >= 0.0 && v <= 100.0) d_Q_phase = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_KALMAN_Q_RATE")) {
        double v = std::atof(p);
        if (v >= 0.0 && v <= 100.0) d_Q_rate = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_KALMAN_R_BASE")) {
        double v = std::atof(p);
        if (v > 0.0 && v <= 1.0e6) d_R_base = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_KALMAN_INIT_P")) {
        double v = std::atof(p);
        if (v > 0.0 && v <= 1.0e9) d_init_P = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_KALMAN_BYPASS")) {
        d_kalman_bypass = (std::atoi(p) != 0);
    }
    if (const char* p = std::getenv("ATSC_SYNC_KALMAN_SNR_EMA")) {
        double v = std::atof(p);
        if (v > 0.0 && v <= 1.0) d_snr_ema_alpha = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_KALMAN_UNLOCK_STREAK")) {
        int v = std::atoi(p);
        if (v >= 1 && v <= 32) d_unlock_streak_req = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_KALMAN_EMIT_UNLOCKED")) {
        d_emit_when_unlocked = (std::atoi(p) != 0);
    }
    if (const char* p = std::getenv("ATSC_SYNC_KALMAN_DEBUG")) {
        d_debug = (std::atoi(p) != 0);
    }

    d_segs_emitted = 0;
    d_segs_held = 0;
    d_segs_aligned = 0;
    d_segs_total = 0;
    d_relocks = 0;
    d_seg_count = 0;
    d_kf_updates = 0;

    std::fprintf(stderr,
                 "[sync_kalman] rate=%.0f alpha=%.4f lock=%.2f unlock=%.2f "
                 "sticky=%.2f timing_scale=%.2f Q_phase=%.4g Q_rate=%.4g "
                 "R_base=%.4g init_P=%.4g snr_ema=%.2f unlock_streak=%d "
                 "bypass=%d emit_unlocked=%d debug=%d\n",
                 rate, d_alpha, d_lock_threshold, d_unlock_threshold,
                 d_sticky_fraction, d_timing_gain_scale, d_Q_phase, d_Q_rate,
                 d_R_base, d_init_P, d_snr_ema_alpha, d_unlock_streak_req,
                 (int)d_kalman_bypass,
                 (int)d_emit_when_unlocked, (int)d_debug);

    reset();
}

void atsc_sync_kalman_impl::reset_kalman()
{
    d_kf_phase = 0.0;
    d_kf_rate  = 0.0;
    d_kf_P00   = d_init_P;
    d_kf_P01   = 0.0;
    d_kf_P11   = d_init_P;
    d_timing_adjust_smooth = 0.0;
}

void atsc_sync_kalman_impl::reset()
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
    reset_kalman();
}

atsc_sync_kalman_impl::~atsc_sync_kalman_impl()
{
    double align_pct = (d_segs_emitted > 0)
        ? 100.0 * (double)d_segs_aligned / (double)d_segs_emitted
        : 0.0;
    std::fprintf(stderr,
                 "[sync_kalman FINAL] segs_emitted=%llu segs_held=%llu "
                 "segs_aligned=%llu (%.2f%%) relocks=%llu kf_updates=%llu\n",
                 (unsigned long long)d_segs_emitted,
                 (unsigned long long)d_segs_held,
                 (unsigned long long)d_segs_aligned,
                 align_pct,
                 (unsigned long long)d_relocks,
                 (unsigned long long)d_kf_updates);
}

void atsc_sync_kalman_impl::forecast(int noutput_items,
                                     gr_vector_int& ninput_items_required)
{
    unsigned ninputs = ninput_items_required.size();
    for (unsigned i = 0; i < ninputs; i++)
        ninput_items_required[i] =
            static_cast<int>(noutput_items * d_rx_clock_to_symbol_freq *
                             ATSC_DATA_SEGMENT_LENGTH) + 1500 - 1;
}

int atsc_sync_kalman_impl::general_work(int noutput_items,
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

        // Apply Kalman-smoothed timing adjustment when locked, raw
        // measurement when unlocked. The raw d_timing_adjust drives
        // initial lock acquisition even with noisy/non-converged signal;
        // suppressing it during unlock would prevent re-lock. Only
        // SUBSTITUTE the smoothed value once a stable lock is held.
        const double t_gain = d_seg_locked
            ? (double)d_timing_gain_scale : 1.0;
        const double t_value = (d_kalman_bypass || !d_seg_locked)
            ? d_timing_adjust : d_timing_adjust_smooth;
        d_mu += t_gain * ADJUSTMENT_GAIN * 1e3 * t_value;

        double s = d_mu + d_w;
        double float_incr = std::floor(s);
        d_mu = s - float_incr;
        d_incr = (int)float_incr;
        if (d_incr < 1) d_incr = 1;
        if (d_incr > 3) d_incr = 3;
        d_si += d_incr;

        d_sample_mem[d_counter] = interp_sample;

        // Update 4-sample matched-filter FIFO. Same convention as soft.
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
                if (v > best_val) {
                    best_val = v;
                    best_idx = i;
                }
            }
            const double rms = std::sqrt(sum_sq / (double)ATSC_DATA_SEGMENT_LENGTH);
            const double snr_ratio = (rms > 1e-9) ? (double)best_val / rms : 0.0;

            // Sticky-lock + constrained search: same logic as atsc_sync_soft
            // but with optional wider search window.
            if (d_seg_locked && d_locked_idx >= 0 &&
                d_locked_idx < ATSC_DATA_SEGMENT_LENGTH) {
                static int SEARCH_W = []{
                    if (const char* p = std::getenv("ATSC_SYNC_KALMAN_SEARCH_W")) {
                        int v = std::atoi(p);
                        if (v >= 1 && v <= 416) return v;
                    }
                    return 6;
                }();
                int local_best_idx = d_locked_idx;
                float local_best_val = d_integrator[d_locked_idx];
                for (int d = -SEARCH_W; d <= SEARCH_W; d++) {
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
                    // Stay in sticky branch. But only MOVE d_locked_idx
                    // within the window if local_best_val clearly exceeds
                    // current locked value. Without this guard, EMA noise
                    // makes neighboring bins briefly higher and the lock
                    // wobbles by ±1 bin every segment — misaligning the
                    // seg-sync at the emitted output.
                    if (local_best_idx != d_locked_idx &&
                        local_best_val >= d_local_move_factor * locked_val) {
                        d_locked_idx = local_best_idx;
                        best_idx = local_best_idx;
                        best_val = local_best_val;
                    } else {
                        // Stay put — emit aligned to current d_locked_idx.
                        best_idx = d_locked_idx;
                        best_val = locked_val;
                    }
                } else {
                    d_locked_idx = best_idx;
                    d_relocks++;
                    // Sticky-lock bin jumped (within continuous lock).
                    // Don't reset Kalman state — at lock, d_timing_adjust
                    // hovers ~0 regardless of which exact bin we're on,
                    // and a reset would force the filter back to high P
                    // and re-converge over many segments. Per-bin jumps
                    // happen every ~10-20 segs on this signal, so reset
                    // would wipe the filter's smoothing benefit.
                }
            }

            // EMA-smoothed snr_ratio for lock decisions: instantaneous
            // snr_ratio dips on every fade/multipath null, prematurely
            // breaking lock. The EMA + streak counter holds lock through
            // brief transients while still releasing on sustained loss.
            d_snr_ema = (1.0 - d_snr_ema_alpha) * d_snr_ema +
                        d_snr_ema_alpha * snr_ratio;

            const bool was_locked = d_seg_locked;
            if (d_seg_locked) {
                // Update streak: count consecutive segs below unlock thr.
                if (snr_ratio < d_unlock_threshold) d_low_snr_streak++;
                else d_low_snr_streak = 0;

                // Hold lock while EMA is above threshold OR streak short.
                const bool hold = (d_snr_ema >= d_unlock_threshold) ||
                                  (d_low_snr_streak < d_unlock_streak_req);
                d_seg_locked = hold;
                if (!d_seg_locked) {
                    d_locked_idx = -1;
                    reset_kalman();
                    d_low_snr_streak = 0;
                }
            } else {
                d_low_snr_streak = 0;
                d_seg_locked = (snr_ratio >= d_lock_threshold);
                if (d_seg_locked) {
                    d_locked_idx = best_idx;
                    if (!was_locked) {
                        d_relocks++;
                        reset_kalman();  // fresh start on lock acquire
                        d_snr_ema = snr_ratio; // seed EMA at acquisition
                    }
                }
            }

            // Timing-error gradient at the lock point (raw measurement).
            // Identical to atsc_sync_soft / dtv.atsc_sync formula.
            int corr_count = best_idx;
            d_timing_adjust = -d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = ATSC_DATA_SEGMENT_LENGTH - 1;
            d_timing_adjust -= d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = ATSC_DATA_SEGMENT_LENGTH - 1;
            d_timing_adjust += d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = ATSC_DATA_SEGMENT_LENGTH - 1;
            d_timing_adjust += d_sample_mem[corr_count--];

            // -------- Kalman update --------
            // Predict: F = [[1,1],[0,1]]
            //   x_pred = [phase + rate, rate]
            //   P_pred = F P F^T + Q
            //          = [[P00 + 2 P01 + P11 + Q_phase,  P01 + P11],
            //             [P01 + P11,                     P11 + Q_rate]]
            const double phase_pred = d_kf_phase + d_kf_rate;
            const double rate_pred  = d_kf_rate;
            const double P00_pred = d_kf_P00 + 2.0 * d_kf_P01 + d_kf_P11 + d_Q_phase;
            const double P01_pred = d_kf_P01 + d_kf_P11;
            const double P11_pred = d_kf_P11 + d_Q_rate;

            if (d_seg_locked && snr_ratio > 1e-9 && !d_kalman_bypass) {
                // Update with measurement z = d_timing_adjust, H = [1, 0]
                // R scales with 1/snr^2: stronger lock → trust measurement more.
                const double R = d_R_base / (snr_ratio * snr_ratio);
                const double S = P00_pred + R;
                if (S > 1e-12) {
                    const double K0 = P00_pred / S;
                    const double K1 = P01_pred / S;
                    const double innov = d_timing_adjust - phase_pred;
                    d_kf_phase = phase_pred + K0 * innov;
                    d_kf_rate  = rate_pred  + K1 * innov;
                    d_kf_P00 = (1.0 - K0) * P00_pred;
                    d_kf_P01 = (1.0 - K0) * P01_pred;
                    d_kf_P11 = P11_pred - K1 * P01_pred;
                    d_kf_updates++;
                } else {
                    d_kf_phase = phase_pred;
                    d_kf_rate  = rate_pred;
                    d_kf_P00 = P00_pred;
                    d_kf_P01 = P01_pred;
                    d_kf_P11 = P11_pred;
                }
            } else {
                // Unlocked or bypass: still propagate (predict only). When
                // bypass, the smoothed value isn't used anyway, but we keep
                // state coherent for re-enable.
                d_kf_phase = phase_pred;
                d_kf_rate  = rate_pred;
                d_kf_P00 = P00_pred;
                d_kf_P01 = P01_pred;
                d_kf_P11 = P11_pred;
            }

            // The value the per-sample d_mu loop should integrate. Use the
            // posterior phase estimate; rate is already absorbed into phase
            // by the predict step at each segment boundary.
            d_timing_adjust_smooth = d_kf_phase;

            d_symbol_index = SYMBOL_INDEX_OFFSET - 1 - best_idx;
            if (d_symbol_index < 0) d_symbol_index += ATSC_DATA_SEGMENT_LENGTH;
            d_counter = 0;

            d_seg_count++;
            if (d_debug && (d_seg_count % 256 == 0)) {
                std::fprintf(stderr,
                             "[sync_kalman] seg=%llu peak=%+.2f rms=%.3f "
                             "snr=%.2f locked=%d best=%d raw=%+.3f kf_ph=%+.3f "
                             "kf_rt=%+.4f kf_P00=%.3f emitted=%llu aligned=%llu (%.2f%%)\n",
                             (unsigned long long)d_seg_count, best_val, rms,
                             snr_ratio, (int)d_seg_locked, best_idx,
                             d_timing_adjust, d_kf_phase, d_kf_rate, d_kf_P00,
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
