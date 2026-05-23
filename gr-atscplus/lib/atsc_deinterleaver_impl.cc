/* -*- c++ -*- */
/*
 * Copyright 2014 Free Software Foundation, Inc.
 * 2026-05-22: copied from gr-dtv with explicit tag forwarding so that
 * atscplus.atsc_viterbi_soft's viterbi_metric tags reach
 * atscplus.atsc_rs_decoder_erasure. The stock block's TPP_ALL default
 * was observed in practice to deliver `tags=0` to rs_erasure even
 * with soft viterbi upstream. This version forwards explicitly.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_deinterleaver_impl.h"
#include <gnuradio/dtv/atsc_consts.h>
#include <gnuradio/io_signature.h>
#include <cstdio>

namespace gr {
namespace atscplus {

atsc_deinterleaver::sptr atsc_deinterleaver::make()
{
    return gnuradio::make_block_sptr<atsc_deinterleaver_impl>();
}

atsc_deinterleaver_impl::atsc_deinterleaver_impl()
    : gr::sync_block(
          "atscplus_atsc_deinterleaver",
          io_signature::make2(2, 2,
                              gr::dtv::ATSC_MPEG_RS_ENCODED_LENGTH * sizeof(uint8_t),
                              sizeof(gr::dtv::plinfo)),
          io_signature::make2(2, 2,
                              gr::dtv::ATSC_MPEG_RS_ENCODED_LENGTH * sizeof(uint8_t),
                              sizeof(gr::dtv::plinfo))),
      alignment_fifo(156),
      d_total_segments(0),
      d_total_tags_forwarded(0),
      d_last_log_segments(0)
{
    m_fifo.reserve(s_interleavers);
    for (int i = 0; i < s_interleavers; i++) {
        m_fifo.emplace_back((s_interleavers - 1 - i) * 4);
    }
    sync();

    // Force TPP_ALL so default behavior is preserved alongside our explicit forwarding.
    // TPP_ONE_TO_ONE for sync block — output[n] gets tags from input[n] on
    // the same port number. Default policy varies between GR versions, so
    // set explicitly. We ALSO call add_item_tag in work() for redundancy.
    set_tag_propagation_policy(block::TPP_ONE_TO_ONE);

    std::fprintf(stderr,
                 "[atsc_deinterleaver] init — tag-forwarding clone (gr-atscplus)\n");
}

atsc_deinterleaver_impl::~atsc_deinterleaver_impl() {}

void atsc_deinterleaver_impl::reset()
{
    sync();
    for (auto& i : m_fifo) {
        i.reset();
    }
}

int atsc_deinterleaver_impl::work(int noutput_items,
                                  gr_vector_const_void_star& input_items,
                                  gr_vector_void_star& output_items)
{
    auto in = static_cast<const uint8_t*>(input_items[0]);
    auto out = static_cast<uint8_t*>(output_items[0]);
    auto plin = static_cast<const gr::dtv::plinfo*>(input_items[1]);
    auto plout = static_cast<gr::dtv::plinfo*>(output_items[1]);

    // Explicit tag forwarding: even though TPP_ALL should do this, observation
    // showed rs_erasure consistently received tags=0 with the stock block. We
    // forward every viterbi_metric tag explicitly at the same sample offset.
    std::vector<tag_t> tags;
    const uint64_t in_start = nitems_read(0);
    const uint64_t in_end   = in_start + (uint64_t)noutput_items;
    get_tags_in_range(tags, 0, in_start, in_end,
                      pmt::intern("viterbi_metric"));
    for (const auto& t : tags) {
        // Output offset = same as input offset since this is a sync_block (1:1).
        add_item_tag(0, t.offset, t.key, t.value);
        d_total_tags_forwarded++;
    }

    for (int i = 0; i < noutput_items; i++) {
        // reset commutator if required using INPUT pipeline info
        if (plin[i].first_regular_seg_p()) {
            sync();
        }

        // remap OUTPUT pipeline info to reflect all data segment end-to-end delay
        plout[i] = gr::dtv::plinfo();
        gr::dtv::plinfo::delay(plout[i], plin[i], s_interleavers);

        // now do the actual deinterleaving
        for (unsigned int j = 0; j < gr::dtv::ATSC_MPEG_RS_ENCODED_LENGTH; j++) {
            out[i * gr::dtv::ATSC_MPEG_RS_ENCODED_LENGTH + j] =
                alignment_fifo.stuff(transform(
                    in[i * gr::dtv::ATSC_MPEG_RS_ENCODED_LENGTH + j]));
        }
    }

    d_total_segments += (uint64_t)noutput_items;

    // Telemetry every ~5 seconds of segments (5s @ ~31000 segs/sec).
    if (d_total_segments - d_last_log_segments >= 150000) {
        std::fprintf(stderr,
                     "[atsc_deinterleaver t=%llu] tags_forwarded=%llu "
                     "(rate=%.2f tags/12segs)\n",
                     (unsigned long long)d_total_segments,
                     (unsigned long long)d_total_tags_forwarded,
                     12.0 * (double)d_total_tags_forwarded / (double)d_total_segments);
        std::fflush(stderr);
        d_last_log_segments = d_total_segments;
    }

    return noutput_items;
}

} /* namespace atscplus */
} /* namespace gr */
