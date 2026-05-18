/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */
/* atsc_rs_decoder_erasure — RS decoder with empirical erasure retry.
 *
 * Drop-in replacement for dtv.atsc_rs_decoder for the (207, 187) shortened
 * Reed-Solomon code in ATSC. After a normal hard-decision decode failure,
 * retries with up to N erasure positions chosen from a rolling histogram
 * of recently-corrected byte positions. Empirically-driven approach to
 * unlocking the 2× erasure-vs-error capacity of RS (2e + f <= 20).
 *
 * Differences from gr-dtv's atsc_rs_decoder:
 *   - 1 input / 1 output (data only; plinfo carried externally)
 *   - Sets TEI flag (output byte 1, bit 7) on uncorrectable packets
 *   - Tunable max_erasures (1..20)
 *   - Periodic stderr telemetry
 */
#ifndef INCLUDED_ATSCPLUS_ATSC_RS_DECODER_ERASURE_H
#define INCLUDED_ATSCPLUS_ATSC_RS_DECODER_ERASURE_H

#include <gnuradio/atscplus/api.h>
#include <gnuradio/sync_block.h>

namespace gr {
namespace atscplus {

class ATSCPLUS_API atsc_rs_decoder_erasure : virtual public gr::sync_block
{
public:
    typedef std::shared_ptr<atsc_rs_decoder_erasure> sptr;

    /*!
     * \brief Make a new instance.
     * \param max_erasures Max erasure positions to try on uncorrectable
     *                     blocks (1..20). Default 14 = 2× the typical hard
     *                     correction capacity (7) at the erasure budget cap.
     */
    static sptr make(int max_erasures = 14);

    virtual int num_packets() const = 0;
    virtual int num_errors_corrected() const = 0;
    virtual int num_erasure_decodes() const = 0;
    virtual int num_bad_packets() const = 0;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_RS_DECODER_ERASURE_H */
