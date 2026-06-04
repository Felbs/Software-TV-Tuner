/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_SYNC_KALMAN_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_SYNC_KALMAN_IMPL_H

#include <gnuradio/atscplus/atsc_sync_kalman.h>
#include <gnuradio/dtv/atsc_consts.h>
#include <gnuradio/filter/mmse_fir_interpolator_ff.h>
#include <gnuradio/filter/single_pole_iir.h>
#include <cstdint>

namespace gr {
namespace atscplus {

class atsc_sync_kalman_impl : public atsc_sync_kalman
{
private:
    gr::filter::single_pole_iir<float, float, float> d_loop;
    gr::filter::mmse_fir_interpolator_ff d_interp;

    double d_rx_clock_to_symbol_freq;
    int d_si;
    double d_w;   // Tx/Rx clock period ratio
    double d_mu;  // fractional sample delay [0,1]
    int d_incr;

    float d_sample_mem[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];
    float d_data_mem[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];

    // 4-tap FIFO of recent interpolated symbols for the matched filter.
    float d_mf_buf[4];
    int d_mf_idx;

    double d_timing_adjust;        // raw measurement (this segment)
    double d_timing_adjust_smooth; // Kalman-smoothed value (used per-sample)
    int d_counter;
    int d_symbol_index;
    bool d_seg_locked;

    // Float-precision EMA integrator. d_integrator[k] holds the
    // smoothed matched-filter response observed at counter==k.
    float d_integrator[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];

    int d_output_produced;

    // -------- Soft-detector tunables (inherited semantics) --------
    float d_alpha;             // EMA rate per sample
    float d_lock_threshold;    // peak/RMS to acquire lock
    float d_unlock_threshold;  // peak/RMS to hold lock
    float d_sticky_fraction;   // sticky-lock: stay on d_locked_idx while its value >= sticky*max
    bool d_emit_when_unlocked;
    bool d_debug;
    int d_locked_idx;          // currently-locked argmax bin (-1 = unlocked)
    float d_timing_gain_scale; // multiplier on timing_adjust gain when locked
    // In-window movement threshold: within sticky-lock, only move
    // d_locked_idx to local_best_idx if local_best_val exceeds the
    // current locked value by this fraction. Without this, EMA noise
    // causes 1-2 bin wobble per segment even when truly locked, which
    // misaligns the emitted seg-sync. Default 1.10 means "move only if
    // a competing bin is 10% higher."
    float d_local_move_factor;

    // -------- Kalman filter state --------
    // 2-state: x = [phase, rate]^T
    //   phase: instantaneous timing-error gradient (this is the variable
    //          that the per-sample d_mu loop actually wants — should be ~0
    //          at perfect lock, with slow drift mixed in)
    //   rate:  per-segment drift in phase (slow varying clock-rate offset)
    // Predict (per segment, F = [[1,1],[0,1]]):
    //   phase_pred = phase + rate
    //   rate_pred  = rate
    // Update with measurement z = d_timing_adjust_raw (H = [1, 0]):
    //   K = P H^T / (H P H^T + R)
    //   x = x_pred + K * (z - H x_pred)
    //   P = (I - K H) P_pred
    // R is scaled by 1/snr_ratio^2 so noisy segments pull less.
    double d_kf_phase;     // smoothed phase estimate
    double d_kf_rate;      // smoothed rate estimate
    double d_kf_P00;       // covariance: phase variance
    double d_kf_P01;       // covariance: phase-rate cov
    double d_kf_P11;       // covariance: rate variance

    // Kalman tunables
    double d_Q_phase;    // process variance, phase
    double d_Q_rate;     // process variance, rate
    double d_R_base;     // measurement variance scale (multiplied by 1/snr^2)
    double d_init_P;     // initial uncertainty when (re)acquiring lock
    bool   d_kalman_bypass; // if true, skip filter (use raw measurement)

    // SNR EMA + hysteresis: smooth peak/RMS over multiple segments and
    // require N consecutive low-SNR segs before declaring unlock. The
    // raw single-segment snr_ratio dips on every transient (deep fade,
    // multipath null), causing lock to drop and the system to spend
    // ~10% of segments unlocked. Smoothing + hysteresis keeps lock held
    // through transients without slowing initial acquisition.
    double d_snr_ema;
    double d_snr_ema_alpha;     // 0..1, EMA gain on snr_ratio
    int    d_low_snr_streak;    // consecutive segments with snr < unlock thr
    int    d_unlock_streak_req; // consecutive low-SNR segs needed to unlock

    // Stats
    uint64_t d_segs_emitted;
    uint64_t d_segs_held;
    uint64_t d_segs_aligned;   // sign(out[0..3]) == +,-,-,+
    uint64_t d_segs_total;
    uint64_t d_relocks;
    uint64_t d_seg_count;      // increments every 832 samples (for periodic logging)
    uint64_t d_kf_updates;     // count of Kalman updates applied

public:
    atsc_sync_kalman_impl(float rate);
    ~atsc_sync_kalman_impl() override;

    void reset();
    void reset_kalman();

    void forecast(int noutput_items, gr_vector_int& ninput_items_required) override;

    int general_work(int noutput_items,
                     gr_vector_int& ninput_items,
                     gr_vector_const_void_star& input_items,
                     gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_SYNC_KALMAN_IMPL_H */
