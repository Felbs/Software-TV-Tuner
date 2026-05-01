/* -*- c++ -*- */
/*
 * Copyright 2014 Free Software Foundation, Inc.
 *
 * This file is part of GNU Radio
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_VITERBI_SOFT_H
#define INCLUDED_ATSCPLUS_ATSC_VITERBI_SOFT_H

#include <gnuradio/atscplus/api.h>
#include <gnuradio/sync_block.h>

namespace gr {
namespace atscplus {

/*!
 * \brief ATSC Viterbi Decoder
 *
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_viterbi_soft : virtual public gr::sync_block
{
public:
    // gr::dtv::atsc_viterbi_soft::sptr
    typedef std::shared_ptr<atsc_viterbi_soft> sptr;

    /*!
     * \brief Make a new instance of gr::dtv::atsc_viterbi_soft.
     */
    static sptr make();

    /*!
     * For each decoder, returns the current best state of the
     * decoding metrics.
     */
    virtual std::vector<float> decoder_metrics() const = 0;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_VITERBI_SOFT_H */
