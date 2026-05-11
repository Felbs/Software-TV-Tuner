/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_DD_H
#define INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_DD_H

#include <gnuradio/block.h>
#include <gnuradio/atscplus/api.h>

namespace gr {
namespace atscplus {

/*!
 * \brief ATSC pilot-LS + decision-directed channel equalizer.
 *
 * Sibling of atsc_equalizer_pilot: re-solves taps via closed-form
 * ridge LS at every Field Sync, AND between Field Syncs runs an
 * LMS-style decision-directed update on each emitted data segment.
 * Slicer maps output to nearest 8-VSB level {±1,±3,±5,±7} and the
 * residual error drives a small-step gradient on the FIR taps.
 *
 * Tunable env vars:
 *   PILOT_DD_MU       — LMS step (default 3e-4)
 *   PILOT_DD_GATE     — max |slicer error| accepted for update (default 2.0)
 *   PILOT_DD_LEAK     — per-segment tap leakage (default 0.0)
 *   PILOT_DD_RESET_FS — reset to pure-LS at each FS (1=on, default 1)
 *   PILOT_DD_RIDGE    — LS ridge (default 1e-2; same as pilot)
 *   PILOT_DD_DEBUG    — verbose stderr per FS / per 256 segs (default 0)
 *
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_equalizer_pilot_dd : virtual public gr::block
{
public:
    typedef std::shared_ptr<atsc_equalizer_pilot_dd> sptr;

    static sptr make();

    virtual std::vector<float> taps() const = 0;
    virtual std::vector<float> data() const = 0;
    virtual float last_residual_rms() const = 0;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_DD_H */
