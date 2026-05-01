/* -*- c++ -*- */
/*
 * Copyright 2014 Free Software Foundation, Inc.
 *
 * This file is part of GNU Radio
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_SYNC_TUNABLE_H
#define INCLUDED_ATSCPLUS_ATSC_SYNC_TUNABLE_H

#include <gnuradio/atscplus/api.h>
#include <gnuradio/sync_block.h>

namespace gr {
namespace atscplus {

/*!
 * \brief ATSC Receiver SYNC
 *
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_sync_tunable : virtual public gr::block
{
public:
    // gr::dtv::atsc_sync_tunable::sptr
    typedef std::shared_ptr<atsc_sync_tunable> sptr;

    /*!
     * \brief Make a new instance of gr::dtv::atsc_sync_tunable.
     *
     * param rate  Sample rate of incoming stream
     */
    static sptr make(float rate, int min_lock_corr = 3, int unlock_corr = 1, bool emit_when_unlocked = true);
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_SYNC_TUNABLE_H */
