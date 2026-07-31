/* -*- c++ -*- */
/*
 * gr-atscplus — widely-linear ATSC 8-VSB equalizer (2026-07-26)
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Widely-linear equalizer for the IMPROPER (noncircular) 8-VSB baseband. Unlike
 * the strictly-linear equalizers it filters BOTH the complex input x AND its
 * conjugate x*, recovering the improper-signal gain (Picinbono 1995; Schreier &
 * Scharf 2010). Complex input (carrier-corrected, symbol-timed, per-segment) ->
 * real 8-VSB soft symbols out. First-for-ATSC. See lab/equalizer/README.md.
 */
#ifndef INCLUDED_ATSCPLUS_ATSC_EQUALIZER_WL_H
#define INCLUDED_ATSCPLUS_ATSC_EQUALIZER_WL_H

#include <gnuradio/atscplus/api.h>
#include <gnuradio/block.h>
#include <memory>
#include <vector>

namespace gr {
namespace atscplus {

/*!
 * \brief Widely-linear ATSC receiver equalizer (complex in, real out).
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_equalizer_wl : virtual public gr::block
{
public:
    typedef std::shared_ptr<atsc_equalizer_wl> sptr;

    static sptr make();

    //! [ |w1[0..N-1]| , |w2[0..N-1]| ] — main then conjugate-branch tap magnitudes
    virtual std::vector<float> taps() const = 0;
    //! last equalized output segment (telemetry)
    virtual std::vector<float> data() const = 0;
    //! ||w2||^2 / (||w1||^2 + ||w2||^2) — the widely-linear "second look" fraction
    virtual float conj_energy_fraction() const = 0;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_EQUALIZER_WL_H */
