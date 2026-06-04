/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_SYNC_PATHA_H
#define INCLUDED_ATSCPLUS_ATSC_SYNC_PATHA_H

#include <gnuradio/atscplus/api.h>
#include <gnuradio/block.h>

namespace gr {
namespace atscplus {

/*!
 * \brief Path A: FS-anchored phase prediction + locked-idx freeze under fades.
 *
 * Sibling to atsc_sync_slidefs. Same continuous 1664-sample symbol ring
 * + sliding 832-tap Field-Sync template, but turns FS observations into
 * d_symbol_index resets at the moment of detection (not per-iteration
 * extrapolation that fights soft's per-segment relock loop).
 *
 * Two structural changes vs slidefs:
 *   (a) On each FS detection, FORCE d_symbol_index and d_locked_idx to
 *       the FS-derived expected values. This re-anchors absolute phase
 *       once per ~313 segments and bypasses noise-driven argmax drift.
 *   (b) Between FSes, FREEZE d_locked_idx updates when matched-filter
 *       SNR is below a threshold. During fades, the MF argmax wanders
 *       by 1-2 bins; freezing it prevents 1-2 sample mis-positioning of
 *       d_data_mem layout.
 *
 * Tunables (ATSC_SYNC_PA_* env vars):
 *   ATSC_SYNC_PA_FS_THR        FS detection threshold (default 2500.0)
 *   ATSC_SYNC_PA_FREEZE_SNR    SNR ratio below which locked_idx is frozen (default 3.0)
 *   ATSC_SYNC_PA_FS_RESET      0 = FS observation only, 1 = FS resets d_symbol_index (default 1)
 *   ATSC_SYNC_PA_ALPHA         per-segment EMA rate for 4-tap (default 0.40)
 *   ATSC_SYNC_PA_LOCK          4-tap acquire peak/RMS (default 4.0)
 *   ATSC_SYNC_PA_UNLOCK        4-tap hold peak/RMS (default 2.0)
 *   ATSC_SYNC_PA_STICKY        sticky-lock fraction (default 0.95)
 *   ATSC_SYNC_PA_TIMING_SCALE  timing-adjust gain scale (default 1.0)
 *   ATSC_SYNC_PA_TIMING_FREEZE 1 = also freeze d_timing_adjust under low SNR (default 1)
 *   ATSC_SYNC_PA_FS_PERIOD     check FS every N symbols once locked (default 832)
 *   ATSC_SYNC_PA_FS_BOOTSTRAP  check FS every N symbols pre-lock (default 832)
 *   ATSC_SYNC_PA_FS_HOLD       segs to keep FS lock w/o new FS (default 626)
 *   ATSC_SYNC_PA_EMIT_UNLOCKED emit segments while unlocked (default 1)
 *   ATSC_SYNC_PA_DEBUG         per-1024-segment + per-FS log (default 0)
 *
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_sync_pathA : virtual public gr::block
{
public:
    typedef std::shared_ptr<atsc_sync_pathA> sptr;
    static sptr make(float rate);
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_SYNC_PATHA_H */
