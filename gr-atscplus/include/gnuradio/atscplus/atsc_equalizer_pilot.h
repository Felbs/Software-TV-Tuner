/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_H
#define INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_H

#include <gnuradio/block.h>
#include <gnuradio/atscplus/api.h>

namespace gr {
namespace atscplus {

/*!
 * \brief ATSC pilot-based block-LS channel equalizer.
 *
 * Drop-in replacement for atsc_equalizer_long. Uses a closed-form
 * ridge least-squares solve over each Field Sync's 704 known training
 * symbols to recompute the FIR taps once per field (~41 Hz), instead
 * of LMS gradient descent. Cold-start convergence is one FS interval.
 *
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_equalizer_pilot : virtual public gr::block
{
public:
    typedef std::shared_ptr<atsc_equalizer_pilot> sptr;

    static sptr make();

    virtual std::vector<float> taps() const = 0;
    virtual std::vector<float> data() const = 0;
    virtual float last_residual_rms() const = 0;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_H */
