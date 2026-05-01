/* -*- c++ -*- */
/*
 * Copyright 2014 Free Software Foundation, Inc.
 *
 * This file is part of GNU Radio
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_FPLL_TIGHT_H
#define INCLUDED_ATSCPLUS_ATSC_FPLL_TIGHT_H

#include <gnuradio/atscplus/api.h>
#include <gnuradio/sync_block.h>

namespace gr {
namespace atscplus {

/*!
 * \brief ATSC Receiver FPLL
 *
 * This block is takes in a complex I/Q baseband stream from the
 * receive filter and outputs the 8-level symbol stream.
 *
 * It does this by first locally generating a pilot tone and
 * complex mixing with the input signal.  This results in the
 * pilot tone shifting to DC and places the signal in the upper
 * sideband.
 *
 * As no information is encoded in the phase of the waveform, the
 * Q channel is then discarded, producing a real signal with the
 * lower sideband restored.
 *
 * The 8-level symbol stream still has a DC offset, and still
 * requires symbol timing recovery.
 *
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_fpll_tight : virtual public gr::sync_block
{
public:
    // gr::dtv::atsc_fpll_tight::sptr
    typedef std::shared_ptr<atsc_fpll_tight> sptr;

    /*!
     * \brief Make a new instance of gr::dtv::atsc_fpll_tight.
     *
     * param rate  Sample rate of incoming stream
     */
    static sptr make(float rate, float alpha = 0.003f, float afc_tau_us = 20.0f);
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_FPLL_TIGHT_H */
