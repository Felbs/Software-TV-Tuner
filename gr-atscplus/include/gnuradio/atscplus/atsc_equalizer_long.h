/* -*- c++ -*- */
/*
 * Copyright 2014 Free Software Foundation, Inc.
 *
 * This file is part of GNU Radio
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_EQUALIZER_LONG_H
#define INCLUDED_ATSCPLUS_ATSC_EQUALIZER_LONG_H

#include <gnuradio/block.h>
#include <gnuradio/atscplus/api.h>

namespace gr {
namespace atscplus {

/*!
 * \brief ATSC Receiver Equalizer
 *
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_equalizer_long : virtual public gr::block
{
public:
    // gr::dtv::atsc_equalizer_long::sptr
    typedef std::shared_ptr<atsc_equalizer_long> sptr;

    /*!
     * \brief Make a new instance of gr::dtv::atsc_equalizer_long.
     */
    static sptr make();

    virtual std::vector<float> taps() const = 0;
    virtual std::vector<float> data() const = 0;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_EQUALIZER_LONG_H */
