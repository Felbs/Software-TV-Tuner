/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */

#ifndef INCLUDED_ATSCPLUS_ATSC_RS_DECODER_ERASURE_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_RS_DECODER_ERASURE_IMPL_H

#include <gnuradio/atscplus/atsc_rs_decoder_erasure.h>
#include <array>
#include <chrono>
#include <cstdint>
#include <string>

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

    // 2026-07-06 miscorrection guard v2: PIDs witnessed on HARD-decoded
    // packets (ground truth). An erasure decode producing an unseen PID
    // is a wrong-codeword solution — reject. Ages via halving so a
    // remux/PID change doesn't blacklist forever.
    std::array<uint16_t, 8192> d_pid_seen{};
    uint32_t d_pid_seen_total = 0;
    int d_guard2_rejects = 0;
    // GMD ladder bookkeeping (2026-07-07): trial-level counts kept apart so
    // era_dec/miscorr/g2rej keep their pre-GMD meanings for sheriff/parsers
    long d_gmd_trials = 0;
    int d_gmd_rej = 0;

    // Stats
    int d_packets;
    int d_errors_corrected;
    int d_erasure_decodes;
    int d_erasure_successes;
    int d_miscorrections;
    int d_bad_packets;

    // Viterbi-confidence integration (Day 2/3 of the soft-Viterbi →
    // tagged-stream → erasure-RS project). The atscplus.atsc_viterbi_soft
    // block emits `viterbi_metric` stream tags every NCODERS segments
    // with the average best_state_metric (higher = less confident).
    // We read those tags here and use them to gate retry aggressiveness.
    double d_recent_metric;     // last-seen viterbi_metric (avg across 12 decoders)
    double d_recent_metric_max; // last-seen viterbi_metric_max (worst decoder)
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
    int d_log_syncok = 0;   // DEAF forensics: framed inputs this window

    // Day 10: persistent histogram across chain restarts.
    static constexpr uint32_t HIST_MAGIC   = 0x52534552;  // "RSER"
    static constexpr uint32_t HIST_VERSION = 1;
    std::string d_hist_path;
    int d_save_period_packets;
    int d_packets_since_save;

    void load_histogram();
    void save_histogram() const;

    // Returns # corrections (>=0) or -1 on uncorrectable. Updates internal
    // histogram on success. out188 is always written (even on failure, with
    // the best-effort buffer contents) so the caller can flag TEI.
    // Day 6: now writes 188 bytes (matching stock dtv.atsc_rs_decoder which
    // outputs the first 188 bytes of the 207-byte codeword — sync byte at
    // offset 0 is preserved by deinterleaver, NOT manually injected).
    int decode_block(const unsigned char* in207, unsigned char* out188,
                 const unsigned char* rel207 = nullptr);

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
