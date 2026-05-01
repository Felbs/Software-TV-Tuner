/* -*- c++ -*- */
/*
 * Copyright 2014 Free Software Foundation, Inc.
 *
 * This file is part of GNU Radio
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_FS_CHECKER_INST_H
#define INCLUDED_ATSCPLUS_ATSC_FS_CHECKER_INST_H

#include <gnuradio/block.h>
#include <gnuradio/atscplus/api.h>

namespace gr {
namespace atscplus {

/*!
 * \brief ATSC Receiver FS_CHECKER
 *
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_fs_checker_inst : virtual public gr::block
{
public:
    // gr::dtv::atsc_fs_checker_inst::sptr
    typedef std::shared_ptr<atsc_fs_checker_inst> sptr;

    /*!
     * \brief Make a new instance of gr::dtv::atsc_fs_checker_inst.
     */
    static sptr make();
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_FS_CHECKER_INST_H */
