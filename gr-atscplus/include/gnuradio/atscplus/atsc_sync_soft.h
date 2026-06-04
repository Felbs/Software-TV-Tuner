/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_SYNC_SOFT_H
#define INCLUDED_ATSCPLUS_ATSC_SYNC_SOFT_H

#include <gnuradio/atscplus/api.h>
#include <gnuradio/block.h>

namespace gr {
namespace atscplus {

/*!
 * \brief Soft-decision matched-filter ATSC segment-sync detector.
 *
 * Drop-in replacement for gr::dtv::atsc_sync. Identical I/O signature
 * (float in, 832-float vectors out) and constructor argument (sample rate).
 *
 * The stock gr-dtv block uses a hard-decision (sample > 0 ? 1 : 0) PN511
 * pattern detector that loses 6 dB of margin vs a true matched filter.
 * This block accumulates the float matched-filter response
 * +x[n-3] - x[n-2] - x[n-1] + x[n] in a 832-bin EMA integrator and locks
 * to the bin with the largest mean correlation. On sdr_RF36.cf32 this
 * lifts segment-alignment from ~71% to >99%.
 *
 * Tunable env vars (defaults are sensible for ~14 dB SNR captures):
 *   ATSC_SYNC_SOFT_ALPHA          EMA rate per segment (default 0.05)
 *   ATSC_SYNC_SOFT_LOCK           lock-on peak/RMS ratio (default 4.0)
 *   ATSC_SYNC_SOFT_UNLOCK         lock-hold peak/RMS ratio (default 2.0)
 *   ATSC_SYNC_SOFT_DEBUG          set to 1 to log per-segment lock/SNR
 *   ATSC_SYNC_SOFT_EMIT_UNLOCKED  set to 0 to suppress segments while unlocked
 *
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_sync_soft : virtual public gr::block
{
public:
    typedef std::shared_ptr<atsc_sync_soft> sptr;

    /*!
     * \brief Make a new instance of gr::atscplus::atsc_sync_soft.
     *
     * \param rate  Sample rate of incoming stream
     */
    static sptr make(float rate);
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_SYNC_SOFT_H */
