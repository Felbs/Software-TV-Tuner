/* -*- c++ -*- */
/*
 * Copyright 2014 Free Software Foundation, Inc.
 *
 * This file is part of GNU Radio
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_types.h"
#include "atsc_viterbi_soft_impl.h"
#include "atsc_viterbi_mux.h"
#include <gnuradio/io_signature.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <pmt/pmt.h>

namespace gr {
namespace atscplus {
using namespace gr::dtv;

atsc_viterbi_soft::sptr atsc_viterbi_soft::make()
{
    return gnuradio::make_block_sptr<atsc_viterbi_soft_impl>();
}

atsc_viterbi_soft_impl::atsc_viterbi_soft_impl()
    : sync_block(
          "dtv_atsc_viterbi_soft",
          io_signature::make2(
              2, 2, sizeof(float) * ATSC_DATA_SEGMENT_LENGTH, sizeof(plinfo)),
          io_signature::make2(
              2, 2, sizeof(unsigned char) * ATSC_MPEG_RS_ENCODED_LENGTH, sizeof(plinfo)))
{
    set_output_multiple(NCODERS);

    /*
     * These fifo's handle the alignment problem caused by the
     * inherent decoding delay of the individual viterbi decoders.
     * The net result is that this entire block has a pipeline latency
     * of 12 complete segments.
     */

    // the -4 is for the 4 sync symbols
    const int fifo_size = ATSC_DATA_SEGMENT_LENGTH - 4 - viterbi[0].delay();
    fifo.reserve(NCODERS);
    for (int i = 0; i < NCODERS; i++)
        fifo.emplace_back(fifo_size);

    reset();
}

atsc_viterbi_soft_impl::~atsc_viterbi_soft_impl() {}

void atsc_viterbi_soft_impl::reset()
{
    for (int i = 0; i < NCODERS; i++)
        fifo[i].reset();
}

std::vector<float> atsc_viterbi_soft_impl::decoder_metrics() const
{
    std::vector<float> metrics(NCODERS);
    for (int i = 0; i < NCODERS; i++)
        metrics[i] = viterbi[i].best_state_metric();
    return metrics;
}

int atsc_viterbi_soft_impl::work(int noutput_items,
                                    gr_vector_const_void_star& input_items,
                                    gr_vector_void_star& output_items)
{
    auto in = static_cast<const float*>(input_items[0]);
    auto out = static_cast<unsigned char*>(output_items[0]);
    auto plin = static_cast<const plinfo*>(input_items[1]);
    auto plout = static_cast<plinfo*>(output_items[1]);

    // ── 2026-07-06 THE SCALPEL ── the corruption/DEAF forensics left
    // exactly one un-alibied organ: this block's internal state (12
    // trellis path memories + alignment fifos). STVT_VIT_CMD_FILE lets
    // the FEC sheriff order a FULL state reset mid-stream — fifos AND
    // decoders (the stock reset() only touched fifos). Poll cost: one
    // fopen attempt per work() call.
    static const char* VIT_CMD = std::getenv("STVT_VIT_CMD_FILE");
    if (VIT_CMD) {
        std::FILE* cf = std::fopen(VIT_CMD, "rb");
        if (cf) {
            std::fclose(cf);
            std::remove(VIT_CMD);
            for (int i = 0; i < NCODERS; i++) {
                fifo[i].reset();
                viterbi[i].reset();
            }
            std::fprintf(stderr,
                         "[viterbi_soft] SCALPEL: full state reset "
                         "(12 decoders + fifos)\n");
        }
    }

    // The way the fs_checker works ensures we start getting packets
    // starting with a field sync, and out input multiple is set to
    // NCODERS, so we should always get a mod-NCODERS first packet
    assert(noutput_items % NCODERS == 0);

    int dbwhere;
    int dbindex;
    int shift;
    float symbols[NCODERS][enco_which_max];
    unsigned char dibits[NCODERS][enco_which_max];

    unsigned char out_copy[OUTPUT_SIZE];
    // ── TIER 9 BUGFIX ─────────────────────────────────────────────
    // Zero-initialize out_copy. Stock gr-dtv's atsc_viterbi_decoder
    // leaves this stack buffer uninitialized and relies on the
    // read-modify-write loop below to fully populate every dibit
    // position before the byte is read for memcpy. That's true on
    // the synth path (where the buffer's prior contents happen to
    // be benign), but on real RF in this build configuration the
    // resulting UB caused 100% TEI on every cold start (Tier 1 exp
    // 03; Tier 7 hypothesis 3; this tier confirmed). Forcing a
    // memset gives well-defined behaviour and reliably works on
    // both synth and real RF.
    std::memset(out_copy, 0, sizeof(out_copy));

    std::vector<tag_t> tags;
    for (int i = 0; i < noutput_items; i += NCODERS) {
        /* Build a continuous symbol buffer for each encoder */
        for (unsigned int encoder = 0; encoder < NCODERS; encoder++)
            for (unsigned int k = 0; k < enco_which_max; k++)
                symbols[encoder][k] =
                    in[(i + (enco_which_syms[encoder][k] / ATSC_DATA_SEGMENT_LENGTH)) *
                           ATSC_DATA_SEGMENT_LENGTH +
                       enco_which_syms[encoder][k] % ATSC_DATA_SEGMENT_LENGTH];

        /* Now run each of the 12 Viterbi decoders over their subset of
           the input symbols */
        for (unsigned int encoder = 0; encoder < NCODERS; encoder++)
            for (unsigned int k = 0; k < enco_which_max; k++)
                dibits[encoder][k] = viterbi[encoder].decode(symbols[encoder][k]);

        /* Move dibits into their location in the output buffer */
        for (unsigned int encoder = 0; encoder < NCODERS; encoder++) {
            for (unsigned int k = 0; k < enco_which_max; k++) {
                /* Store the dibit into the output data segment */
                dbwhere = enco_which_dibits[encoder][k];
                dbindex = dbwhere >> 3;
                shift = dbwhere & 0x7;
                out_copy[dbindex] = (out_copy[dbindex] & ~(0x03 << shift)) |
                                    (fifo[encoder].stuff(dibits[encoder][k]) << shift);
            } /* Symbols fed into one encoder */
        }     /* Encoders */

        // copy output from contiguous temp buffer into final output
        for (int j = 0; j < NCODERS; j++) {
            memcpy(&out[(i + j) * ATSC_MPEG_RS_ENCODED_LENGTH],
                   &out_copy[j * ATSC_MPEG_RS_ENCODED_LENGTH],
                   ATSC_MPEG_RS_ENCODED_LENGTH * sizeof(out_copy[0]));

            plout[i + j] = plinfo();
            // adjust pipeline info to reflect 12 segment delay
            plinfo::delay(plout[i + j], plin[i + j], NCODERS);
        }

        // 2026-05-18: emit per-segment confidence as stream tag.
        // best_state_metric() rises with accumulating path uncertainty;
        // lower magnitude = more confident. We tag the FIRST output
        // segment of each NCODERS-segment batch with the average across
        // all 12 decoders. Downstream consumers (future erasure-RS) can
        // gate erasure aggressiveness on this signal. Tagging only the
        // first sample of the batch keeps GR overhead low (~1 tag per
        // 12 segments = ~1 tag per 2484 output bytes).
        // 2026-05-27: ALSO emit the WORST-decoder metric. Average across
        // 12 decoders dilutes a single-decoder failure mode (1 broken /
        // 11 healthy ~ same avg as 12 mediocre, but very different bit-
        // error pattern). Max is more responsive to localized impairment.
        float conf_sum = 0.0f;
        float conf_max = 0.0f;
        for (int e = 0; e < NCODERS; e++) {
            const float m = viterbi[e].best_state_metric();
            conf_sum += m;
            if (m > conf_max) conf_max = m;
        }
        const float avg_metric = conf_sum / float(NCODERS);
        const uint64_t tag_offset = nitems_written(0) + i;
        add_item_tag(0,
                     tag_offset,
                     pmt::intern("viterbi_metric"),
                     pmt::from_double(double(avg_metric)));
        add_item_tag(0,
                     tag_offset,
                     pmt::intern("viterbi_metric_max"),
                     pmt::from_double(double(conf_max)));
    }

    return noutput_items;
}

void atsc_viterbi_soft_impl::setup_rpc()
{
#ifdef GR_CTRLPORT
    add_rpc_variable(
        rpcbasic_sptr(new rpcbasic_register_get<atsc_viterbi_soft, std::vector<float>>(
            alias(),
            "decoder_metrics",
            &atsc_viterbi_soft::decoder_metrics,
            pmt::make_f32vector(1, 0),
            pmt::make_f32vector(1, 100000),
            pmt::make_f32vector(1, 0),
            "",
            "Viterbi decoder metrics",
            RPC_PRIVLVL_MIN,
            DISPTIME)));
#endif /* GR_CTRLPORT */
}

} /* namespace atscplus */
} /* namespace gr */
