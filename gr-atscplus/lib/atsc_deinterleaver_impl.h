/* -*- c++ -*- */
/*
 * Copyright 2014 Free Software Foundation, Inc.
 * 2026-05-22: copied from gr-dtv into atscplus, with explicit
 * tag-forwarding for viterbi_metric tags.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_DEINTERLEAVER_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_DEINTERLEAVER_IMPL_H

#include "interleaver_fifo.h"
#include <gnuradio/atscplus/atsc_deinterleaver.h>
#include <gnuradio/dtv/atsc_plinfo.h>

namespace gr {
namespace atscplus {

class atsc_deinterleaver_impl : public atsc_deinterleaver
{
private:
    static constexpr int s_interleavers = 52;

    unsigned char transform(unsigned char input)
    {
        unsigned char retval = m_fifo[m_commutator].stuff(input);
        m_commutator++;
        if (m_commutator >= s_interleavers)
            m_commutator = 0;
        return retval;
    }

    interleaver_fifo<unsigned char> alignment_fifo;
    int m_commutator;
    std::vector<interleaver_fifo<unsigned char>> m_fifo;

    // Telemetry for the tag-forwarding feature — printed to stderr so we
    // can confirm tags ARE flowing through to rs_erasure.
    uint64_t d_total_segments;
    uint64_t d_total_tags_forwarded;
    uint64_t d_last_log_segments;

public:
    atsc_deinterleaver_impl();
    ~atsc_deinterleaver_impl() override;

    int work(int noutput_items,
             gr_vector_const_void_star& input_items,
             gr_vector_void_star& output_items) override;

    void reset();
    void sync() { m_commutator = 0; }
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_DEINTERLEAVER_IMPL_H */
