/* -*- c++ -*- */
/*
 * gr-atscplus — fused widely-linear front end (2026-07-27)
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * FUSED sync + fs_check + complex-companion carrier for the widely-linear
 * equalizer path (STVT_EQ=wl). Replaces the three-block companion chain
 * (atsc_sync_soft dual-interp -> atsc_fs_checker_inst passthrough) whose
 * per-symbol SECOND mmse_fir_interpolator_ff::interpolate() call was measured
 * to cost +10.3 s per 15 s of air (the entire WL real-time deficit; see
 * lab/wl_fused/WORKLOG.md). Here ONE inlined 8-tap interpolation kernel
 * (taps table extracted from GR's own mmse interpolator at construction, so
 * the values are identical) produces the real AND imaginary symbol in a
 * single pass — no library-call overhead, no parallel companion path.
 *
 * in0  = float real 8-VSB (fpll_tight out0, FOLD mode)
 * in1  = float imaginary companion (fpll_tight out1)
 * out0 = 832-float real segments   -> atsc_equalizer_wl in0
 * out1 = plinfo                    -> atsc_equalizer_wl in1
 * out2 = 832-float imag segments   -> atsc_equalizer_wl in2
 *
 * Timing/lock logic is a verbatim port of atsc_sync_soft (same
 * ATSC_SYNC_SOFT_* env knobs); framing is a verbatim port of
 * atsc_fs_checker_inst (same ATSCPLUS_* env knobs). The default (non-WL)
 * decode path does not use this block and is byte-for-byte unaffected.
 */
#ifndef INCLUDED_ATSCPLUS_ATSC_WL_FRONTEND_H
#define INCLUDED_ATSCPLUS_ATSC_WL_FRONTEND_H

#include <gnuradio/atscplus/api.h>
#include <gnuradio/block.h>

namespace gr {
namespace atscplus {

/*!
 * \brief Fused segment-sync + field-sync framing + complex companion for the
 * widely-linear ATSC equalizer.
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_wl_frontend : virtual public gr::block
{
public:
    typedef std::shared_ptr<atsc_wl_frontend> sptr;

    /*!
     * \param rate  Sample rate of the incoming (post-fpll) stream.
     */
    static sptr make(float rate);
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_WL_FRONTEND_H */
