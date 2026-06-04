/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_DD_SOFT_H
#define INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_DD_SOFT_H

#include <gnuradio/block.h>
#include <gnuradio/atscplus/api.h>

namespace gr {
namespace atscplus {

/*!
 * \brief ATSC pilot-LS + soft-confidence-weighted DD channel equalizer.
 *
 * Sibling of atsc_equalizer_pilot_dd. Differs only in the DD update rule:
 * instead of a hard binary gate (|e| > gate -> skip; |e| <= gate -> full
 * gradient), each sample's gradient is weighted by a soft confidence in
 * [0, 1]. Confidence is high when the sample is unambiguously on an
 * 8-VSB level and falls to 0 near a decision boundary.
 *
 * Default conf(y) is linear-triangular:
 *   e    = y - slice(y)                  (slicer residual; |e| <= 1.0 at boundaries)
 *   conf = max(0, 1 - |e| / GATE)        (1 at level, 0 at boundary)
 *
 * Tunable env vars (defaults differ from pilot_dd because soft weighting
 * permits a larger nominal step):
 *   PILOT_DD_SOFT_MU         LMS step (default 1e-3)
 *   PILOT_DD_SOFT_GATE       conf reaches 0 at this |e| (default 1.0)
 *   PILOT_DD_SOFT_LEAK       per-segment tap leakage (default 0)
 *   PILOT_DD_SOFT_RESET_FS   reset to LS at each FS (1=on, default 1)
 *   PILOT_DD_SOFT_RIDGE      LS ridge (default 1e-2; same as pilot)
 *   PILOT_DD_SOFT_DEBUG      verbose stderr per FS (default 0)
 *
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_equalizer_pilot_dd_soft : virtual public gr::block
{
public:
    typedef std::shared_ptr<atsc_equalizer_pilot_dd_soft> sptr;

    static sptr make();

    virtual std::vector<float> taps() const = 0;
    virtual std::vector<float> data() const = 0;
    virtual float last_residual_rms() const = 0;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_DD_SOFT_H */
