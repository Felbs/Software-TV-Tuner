/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_SYNC_FIELDLOCK_H
#define INCLUDED_ATSCPLUS_ATSC_SYNC_FIELDLOCK_H

#include <gnuradio/atscplus/api.h>
#include <gnuradio/block.h>

namespace gr {
namespace atscplus {

/*!
 * \brief Field-Sync-anchored ATSC segment-sync detector.
 *
 * Drop-in replacement for gr::dtv::atsc_sync. Identical I/O signature
 * (float in, 832-float vectors out) and constructor argument (sample rate).
 *
 * Inherits the soft 4-tap matched-filter detector from atsc_sync_soft as
 * a base, but ADDITIONALLY runs an ~641-tap Field Sync template
 * correlation against each just-emitted segment. The FS template covers
 * the deterministic portion of an ATSC Field Sync segment:
 *   pos 0-3:   segment-sync   [+1, -1, -1, +1]   (4 active)
 *   pos 4-514: PN511 mapped to ±1                (511 active)
 *   pos 515-577: PN63 mapped                     (63 active)
 *   pos 578-640: ZEROED (variable middle PN63, sign-flips between fields)
 *   pos 641-703: PN63 mapped                     (63 active)
 *   pos 704-727: ZEROED (VSB mode bits)
 *   pos 728-831: ZEROED (reserved/precode)
 *
 * On each emitted segment, FS correlation is computed at offsets
 * [-W, +W] around d_locked_idx. When the peak exceeds threshold, this
 * segment IS a Field Sync; the argmax offset (if non-zero) is used to
 * correct the segment-sync bin alignment, and a Field-Sync lock is
 * established that holds the bin position through brief 4-tap fades.
 *
 * Tunable env vars (defaults tuned on sdr_RF36.cf32):
 *   ATSC_SYNC_FL_ALPHA          EMA rate per segment (default 0.40)
 *   ATSC_SYNC_FL_LOCK           4-tap acquire peak/RMS (default 4.0)
 *   ATSC_SYNC_FL_UNLOCK         4-tap hold peak/RMS (default 2.0)
 *   ATSC_SYNC_FL_STICKY         sticky-lock fraction (default 0.95)
 *   ATSC_SYNC_FL_TIMING_SCALE   timing-adjust gain scale (default 1.0)
 *   ATSC_SYNC_FL_LOCAL_MOVE     within-window move threshold (default 1.10)
 *   ATSC_SYNC_FL_SEARCH_W       sticky-lock half-width (default 6)
 *   ATSC_SYNC_FL_FS_W           FS correlation half-width (default 5)
 *   ATSC_SYNC_FL_FS_THR         FS detection threshold (default 800.0)
 *   ATSC_SYNC_FL_FS_HOLD        segs to hold FS lock w/o new FS (default 626)
 *   ATSC_SYNC_FL_FS_DRIVE       1=FS drives bin during FS lock (default 1)
 *   ATSC_SYNC_FL_FS_HOLDLOCK    1=FS lock inhibits 4-tap unlock (default 1)
 *   ATSC_SYNC_FL_FS_CORRECT     1=apply argmax bin correction (default 1)
 *   ATSC_SYNC_FL_EMIT_UNLOCKED  emit segments while unlocked (default 1)
 *   ATSC_SYNC_FL_DEBUG          per-256-segment log (default 0)
 *
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_sync_fieldlock : virtual public gr::block
{
public:
    typedef std::shared_ptr<atsc_sync_fieldlock> sptr;

    /*!
     * \brief Make a new instance of gr::atscplus::atsc_sync_fieldlock.
     *
     * \param rate  Sample rate of incoming stream
     */
    static sptr make(float rate);
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_SYNC_FIELDLOCK_H */
