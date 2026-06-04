/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_MULTIFS_DD_H
#define INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_MULTIFS_DD_H

#include <gnuradio/block.h>
#include <gnuradio/atscplus/api.h>

namespace gr {
namespace atscplus {

/*!
 * \brief K-FS coherent LS equalizer + soft-confidence DD between solves.
 *
 * Combines atsc_equalizer_pilot_multifs (K-FS coherent ridge LS) with
 * atsc_equalizer_pilot_dd_soft (per-symbol confidence-weighted LMS).
 * Intended use: K=4 LS for stable tap initialization, soft-DD with small
 * mu for between-FS channel tracking. Tap-energy bail with restore from
 * last LS solution.
 *
 * Tunables:
 *   PILOT_MFSDD_K          K-FS LS window (default 4)
 *   PILOT_MFSDD_RIDGE      LS ridge (default 1e-2)
 *   PILOT_MFSDD_MU         soft-DD step (default 5e-5)
 *   PILOT_MFSDD_GATE       soft-DD conf 0 boundary (default 1.0)
 *   PILOT_MFSDD_DEBUG      verbose (default 0)
 *
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_equalizer_pilot_multifs_dd : virtual public gr::block
{
public:
    typedef std::shared_ptr<atsc_equalizer_pilot_multifs_dd> sptr;

    static sptr make();

    virtual std::vector<float> taps() const = 0;
    virtual std::vector<float> data() const = 0;
    virtual float last_residual_rms() const = 0;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_MULTIFS_DD_H */
