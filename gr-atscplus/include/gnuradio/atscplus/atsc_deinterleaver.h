/* -*- c++ -*- */
/*
 * Copyright 2014 Free Software Foundation, Inc.
 * 2026-05-22: copied from gr-dtv into atscplus to add explicit
 * stream-tag forwarding for the viterbi_metric tags emitted by
 * atscplus.atsc_viterbi_soft.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_DEINTERLEAVER_H
#define INCLUDED_ATSCPLUS_ATSC_DEINTERLEAVER_H

#include <gnuradio/atscplus/api.h>
#include <gnuradio/sync_block.h>

namespace gr {
namespace atscplus {

/*!
 * \brief ATSC deinterleaver — tag-forwarding clone of gr-dtv's atsc_deinterleaver.
 *
 * Functionally identical to gr::dtv::atsc_deinterleaver: convolutional
 * deinterleaver with 52-way interleaver bank, used between the trellis
 * decoder and Reed-Solomon decoder in the ATSC receive chain. Operates
 * on atsc_mpeg_packet_rs_encoded segments (207 bytes each).
 *
 * Difference from stock: explicit propagation of "viterbi_metric"
 * stream tags emitted by atscplus.atsc_viterbi_soft. The stock block
 * relies on GR's default tag propagation policy (TPP_ALL), but in
 * practice rs_erasure observed `tags=0` even when soft viterbi was
 * upstream. This block explicitly forwards all input tags at the
 * SAME sample offset on the output, which keeps rs_erasure happy.
 *
 * input/output:  segments of ATSC_MPEG_RS_ENCODED_LENGTH (207) bytes
 *                + plinfo metadata stream
 */
class ATSCPLUS_API atsc_deinterleaver : virtual public gr::sync_block
{
public:
    typedef std::shared_ptr<atsc_deinterleaver> sptr;
    static sptr make();
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_DEINTERLEAVER_H */
