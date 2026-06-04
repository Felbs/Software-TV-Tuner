/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_SYNC_SLIDEFS_H
#define INCLUDED_ATSCPLUS_ATSC_SYNC_SLIDEFS_H

#include <gnuradio/atscplus/api.h>
#include <gnuradio/block.h>

namespace gr {
namespace atscplus {

/*!
 * \brief Sliding-Field-Sync ATSC segment-sync block (Option 1.5).
 *
 * Drop-in replacement for gr::dtv::atsc_sync. Identical I/O signature
 * (float in, 832-float vectors out) and constructor argument (sample rate).
 *
 * Architecturally distinct from atsc_sync / atsc_sync_soft / atsc_sync_kalman /
 * atsc_sync_fieldlock: instead of relying on a per-segment 4-tap segsync
 * detector, this block maintains a continuous SYMBOL-TIME ring buffer
 * (1664 = 2 segments) that is filled with EVERY interpolated symbol,
 * independent of segment-cycle state. A sliding 832-tap Field-Sync template
 * correlation runs across the ring and, when a peak above threshold is
 * found, ANCHORS the segment cycle to absolute symbol-time. This bypasses
 * the d_data_mem splicing problem that capped atsc_sync_fieldlock at 91%.
 *
 * The 4-tap segsync EMA is retained as a bootstrap before first FS lock and
 * as a backup drift estimator between FS detections, but FS, when locked,
 * is the authoritative reference.
 *
 * Tunable env vars (defaults tuned on sdr_RF36.cf32):
 *   ATSC_SYNC_SF_FS_THR        FS detection threshold (default 2500.0)
 *   ATSC_SYNC_SF_FS_PERIOD     check FS every N symbols once locked (default 832)
 *   ATSC_SYNC_SF_FS_BOOTSTRAP  check FS every N symbols pre-lock (default 64)
 *   ATSC_SYNC_SF_FS_HOLD       segs to keep FS lock w/o new FS (default 626)
 *   ATSC_SYNC_SF_FS_DRIFT_W    max bin drift between FSes (default 4)
 *   ATSC_SYNC_SF_ALPHA         per-segment EMA rate for 4-tap base (default 0.40)
 *   ATSC_SYNC_SF_LOCK          4-tap acquire peak/RMS (default 4.0)
 *   ATSC_SYNC_SF_UNLOCK        4-tap hold peak/RMS (default 2.0)
 *   ATSC_SYNC_SF_TIMING_SCALE  timing-adjust gain scale (default 1.0)
 *   ATSC_SYNC_SF_EMIT_UNLOCKED emit segments while unlocked (default 1)
 *   ATSC_SYNC_SF_DEBUG         per-256-segment log (default 0)
 *
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_sync_slidefs : virtual public gr::block
{
public:
    typedef std::shared_ptr<atsc_sync_slidefs> sptr;

    /*!
     * \brief Make a new instance of gr::atscplus::atsc_sync_slidefs.
     *
     * \param rate  Sample rate of incoming stream
     */
    static sptr make(float rate);
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_SYNC_SLIDEFS_H */
