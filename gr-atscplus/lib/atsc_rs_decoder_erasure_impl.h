/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */

#ifndef INCLUDED_ATSCPLUS_ATSC_RS_DECODER_ERASURE_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_RS_DECODER_ERASURE_IMPL_H

#include <gnuradio/atscplus/atsc_rs_decoder_erasure.h>
#include <array>
#include <chrono>

namespace gr {
namespace atscplus {

class atsc_rs_decoder_erasure_impl : public atsc_rs_decoder_erasure
{
private:
    // RS over GF(256), shortened (207, 187) from full (255, 235).
    // Decode by prepending 48 implicit-zero bytes to the 207-byte received
    // codeword and calling the (255, 235) routine.
    static constexpr int RS_N        = 255;
    static constexpr int RS_NROOTS   = 20;
    static constexpr int PAD_BYTES   = 48;
    static constexpr int CODE_LEN    = 207;   // input  vector size (RS-encoded)
    static constexpr int DATA_LEN    = 187;   // RS-decoded message length
    static constexpr int PKT_LEN     = 188;   // output vector size (sync + data)

    void* d_rs;
    int   d_max_erasures;

    // Rolling histogram of correction positions in the 207-byte codeword
    // space. Updated each time a normal-decode succeeds with corrections.
    // Periodically decayed so the histogram tracks slow channel changes.
    int   d_hist_count;          // # successful blocks contributing
    int   d_hist_decay_period;   // packets between decays
    std::array<int, CODE_LEN> d_hist_pos;

    // Stats
    int d_packets;
    int d_errors_corrected;
    int d_erasure_decodes;
    int d_erasure_successes;
    int d_bad_packets;

    // Viterbi-confidence integration (Day 2/3 of the soft-Viterbi →
    // tagged-stream → erasure-RS project). The atscplus.atsc_viterbi_soft
    // block emits `viterbi_metric` stream tags every NCODERS segments
    // with the average best_state_metric (higher = less confident).
    // We read those tags here and use them to gate retry aggressiveness.
    double d_recent_metric;     // last-seen viterbi_metric value
    int    d_metric_tag_count;  // total tags observed (sanity check)
    int    d_effective_max_erasures;  // metric-gated, updated per work()

    // Day 3: derive a dynamic erasure budget from d_recent_metric.
    // Empirical thresholds based on observed range 3000-7000 (typical
    // marginal-RF lock at ~5000). Lower = more confident signal.
    int dynamic_max_erasures() const;

    // Periodic stderr log
    std::chrono::steady_clock::time_point d_t0;
    std::chrono::steady_clock::time_point d_last_log;
    int d_log_packets;
    int d_log_eras_dec;
    int d_log_eras_ok;
    int d_log_bad;

    // Returns # corrections (>=0) or -1 on uncorrectable. Updates internal
    // histogram on success. out187 is always written (even on failure, with
    // the best-effort buffer contents) so the caller can flag TEI.
    int decode_block(const unsigned char* in207, unsigned char* out187);

public:
    atsc_rs_decoder_erasure_impl(int max_erasures);
    ~atsc_rs_decoder_erasure_impl() override;

    int num_packets()           const override { return d_packets; }
    int num_errors_corrected()  const override { return d_errors_corrected; }
    int num_erasure_decodes()   const override { return d_erasure_decodes; }
    int num_bad_packets()       const override { return d_bad_packets; }

    int work(int noutput_items,
             gr_vector_const_void_star& input_items,
             gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif
