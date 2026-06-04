/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_SYNC_KALMAN_H
#define INCLUDED_ATSCPLUS_ATSC_SYNC_KALMAN_H

#include <gnuradio/atscplus/api.h>
#include <gnuradio/block.h>

namespace gr {
namespace atscplus {

/*!
 * \brief Kalman-filter timing-recovery ATSC segment-sync detector.
 *
 * Drop-in replacement for gr::dtv::atsc_sync. Identical I/O signature
 * (float in, 832-float vectors out) and constructor argument (sample rate).
 *
 * Inherits the soft matched-filter detector and EMA integrator from
 * gr::atscplus::atsc_sync_soft, but replaces the raw per-sample
 * d_timing_adjust integrator with a 2-state Kalman filter on per-segment
 * timing-error gradient measurements. Process model: phase + drifting
 * rate (random walk). Measurement variance R_k is scaled by 1/snr_ratio^2
 * so noisy/unlocked segments pull less. The smoothed phase output is
 * applied per-sample with the same gain as the original loop, eliminating
 * the noise-integration drift that limits atsc_sync_soft to ~91% segment
 * alignment on weak captures.
 *
 * Tunable env vars (defaults tuned on sdr_RF36.cf32):
 *   ATSC_SYNC_KALMAN_ALPHA          EMA rate per segment (default 0.40)
 *   ATSC_SYNC_KALMAN_LOCK           lock-on peak/RMS ratio (default 4.0)
 *   ATSC_SYNC_KALMAN_UNLOCK         lock-hold peak/RMS ratio (default 2.0)
 *   ATSC_SYNC_KALMAN_STICKY         sticky-lock fraction (default 0.95)
 *   ATSC_SYNC_KALMAN_TIMING_SCALE   timing-adjust gain scale (default 1.0)
 *   ATSC_SYNC_KALMAN_Q_PHASE        process noise variance, phase (default 0.05)
 *   ATSC_SYNC_KALMAN_Q_RATE         process noise variance, rate (default 0.0005)
 *   ATSC_SYNC_KALMAN_R_BASE         meas-variance scale (default 5.0)
 *   ATSC_SYNC_KALMAN_INIT_P         initial uncertainty (default 100.0)
 *   ATSC_SYNC_KALMAN_BYPASS         set 1 to bypass filter (≡ soft) (default 0)
 *   ATSC_SYNC_KALMAN_EMIT_UNLOCKED  emit segments while unlocked (default 1)
 *   ATSC_SYNC_KALMAN_DEBUG          per-256-segment log (default 0)
 *
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_sync_kalman : virtual public gr::block
{
public:
    typedef std::shared_ptr<atsc_sync_kalman> sptr;

    /*!
     * \brief Make a new instance of gr::atscplus::atsc_sync_kalman.
     *
     * \param rate  Sample rate of incoming stream
     */
    static sptr make(float rate);
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_SYNC_KALMAN_H */
