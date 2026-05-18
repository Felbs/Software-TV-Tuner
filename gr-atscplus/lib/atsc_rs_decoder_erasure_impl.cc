/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */
/* See atsc_rs_decoder_erasure.h for the algorithmic rationale. */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_rs_decoder_erasure_impl.h"
#include <gnuradio/io_signature.h>
// gnuradio/fec/rs.h is a C header; gr-fec was built without extern "C"
// guards in its public header. Wrap it here so the symbols resolve.
extern "C" {
#include <gnuradio/fec/rs.h>
}

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <vector>

namespace gr {
namespace atscplus {

atsc_rs_decoder_erasure::sptr atsc_rs_decoder_erasure::make(int max_erasures)
{
    return gnuradio::make_block_sptr<atsc_rs_decoder_erasure_impl>(max_erasures);
}

atsc_rs_decoder_erasure_impl::atsc_rs_decoder_erasure_impl(int max_erasures)
    : sync_block("atscplus_atsc_rs_decoder_erasure",
                 io_signature::make(1, 1, CODE_LEN),
                 io_signature::make(1, 1, PKT_LEN))
{
    // Phil Karn / gnuradio-fec init. Parameters MUST match gr-dtv's stock
    // atsc_rs_decoder so the GF and generator polynomials agree.
    d_rs = init_rs_char(8, 0x11d, 0, 1, RS_NROOTS);
    if (!d_rs)
        throw std::runtime_error("init_rs_char failed");

    d_max_erasures      = std::clamp(max_erasures, 1, 20);
    d_hist_count        = 0;
    d_hist_decay_period = 500;   // decay histogram every 500 packets
    d_hist_pos.fill(0);

    d_packets            = 0;
    d_errors_corrected   = 0;
    d_erasure_decodes    = 0;
    d_erasure_successes  = 0;
    d_bad_packets        = 0;

    d_t0       = std::chrono::steady_clock::now();
    d_last_log = d_t0;
    d_log_packets  = 0;
    d_log_eras_dec = 0;
    d_log_eras_ok  = 0;
    d_log_bad      = 0;

    std::fprintf(stderr,
                 "[rs_erasure] init max_erasures=%d decay_period=%d\n",
                 d_max_erasures, d_hist_decay_period);
}

atsc_rs_decoder_erasure_impl::~atsc_rs_decoder_erasure_impl()
{
    if (d_rs)
        free_rs_char(d_rs);
}

int atsc_rs_decoder_erasure_impl::decode_block(const unsigned char* in207,
                                               unsigned char* out187)
{
    // (207, 187) shortened from (255, 235) — 48 byte implicit zero pad
    // at the front of the 255-byte buffer fed to decode_rs_char.
    unsigned char tmp[RS_N];
    std::memset(tmp, 0, PAD_BYTES);
    std::memcpy(tmp + PAD_BYTES, in207, CODE_LEN);

    // First attempt: hard decode (no erasures).
    int n = decode_rs_char(d_rs, tmp, nullptr, 0);
    if (n >= 0) {
        // Update histogram with positions that were corrected.
        // Compare corrected tmp[PAD..PAD+CODE_LEN] against original in207.
        for (int i = 0; i < CODE_LEN; i++) {
            if (tmp[PAD_BYTES + i] != in207[i]) {
                int v = d_hist_pos[i] + 1;
                if (v > 2000) v = 2000;
                d_hist_pos[i] = v;
            }
        }
        std::memcpy(out187, tmp + PAD_BYTES, DATA_LEN);
        d_hist_count++;
        return n;
    }

    // First attempt failed.
    // If we don't have meaningful histogram yet, give up.
    if (d_hist_count < 20) {
        std::memcpy(out187, in207, DATA_LEN);
        return -1;
    }

    // Retry with empirical erasures from the histogram.
    // Pick the top-K positions by correction count.
    std::vector<std::pair<int, int>> ranked;
    ranked.reserve(CODE_LEN);
    for (int i = 0; i < CODE_LEN; i++) {
        if (d_hist_pos[i] > 0)
            ranked.emplace_back(d_hist_pos[i], i);
    }
    if (ranked.empty()) {
        std::memcpy(out187, in207, DATA_LEN);
        return -1;
    }
    std::sort(ranked.begin(), ranked.end(),
              [](const std::pair<int,int>& a, const std::pair<int,int>& b) {
                  return a.first > b.first;
              });

    int no_eras = std::min<int>({d_max_erasures, (int)ranked.size(), 20});
    int eras_pos[20];
    for (int i = 0; i < no_eras; i++)
        eras_pos[i] = ranked[i].second + PAD_BYTES;   // positions in 255-buf

    // Re-fill tmp because decode_rs_char may have modified it.
    std::memset(tmp, 0, PAD_BYTES);
    std::memcpy(tmp + PAD_BYTES, in207, CODE_LEN);

    d_erasure_decodes++;
    d_log_eras_dec++;
    n = decode_rs_char(d_rs, tmp, eras_pos, no_eras);
    if (n >= 0) {
        d_erasure_successes++;
        d_log_eras_ok++;
        // Update histogram: positions actually-corrected (could include some
        // of the erasure positions or different ones). Compare tmp to in207
        // across the full codeword.
        for (int i = 0; i < CODE_LEN; i++) {
            if (tmp[PAD_BYTES + i] != in207[i]) {
                int v = d_hist_pos[i] + 1;
                if (v > 2000) v = 2000;
                d_hist_pos[i] = v;
            }
        }
        std::memcpy(out187, tmp + PAD_BYTES, DATA_LEN);
        d_hist_count++;
        return n;
    }

    // Still uncorrectable — return data as-is for caller to mark TEI.
    std::memcpy(out187, in207, DATA_LEN);
    return -1;
}

int atsc_rs_decoder_erasure_impl::work(int noutput_items,
                                       gr_vector_const_void_star& input_items,
                                       gr_vector_void_star&       output_items)
{
    auto in  = static_cast<const unsigned char*>(input_items[0]);
    auto out = static_cast<unsigned char*>(output_items[0]);

    unsigned char data187[DATA_LEN];

    for (int i = 0; i < noutput_items; i++) {
        const unsigned char* in_pkt  = in  + i * CODE_LEN;
        unsigned char*       out_pkt = out + i * PKT_LEN;

        int n = decode_block(in_pkt, data187);
        d_packets++;
        d_log_packets++;

        // Output is 188-byte TS packet: sync byte + 187 data bytes.
        // (Stock gr-dtv atsc_rs_decoder follows this convention.)
        out_pkt[0] = 0x47;
        std::memcpy(out_pkt + 1, data187, DATA_LEN);

        if (n < 0) {
            // Set TEI flag (bit 7 of byte 1) so teiscrub can NULL-out
            // the packet downstream.
            out_pkt[1] |= 0x80;
            d_bad_packets++;
            d_log_bad++;
        } else if (n > 0) {
            d_errors_corrected += n;
        }
    }

    // Periodic decay of histogram so it tracks slow channel changes.
    if (d_packets % d_hist_decay_period == 0) {
        for (auto& v : d_hist_pos)
            v = (v * 7) / 8;        // 12.5% decay per period
    }

    // Stderr telemetry every 5 s.
    auto now = std::chrono::steady_clock::now();
    long dt = std::chrono::duration_cast<std::chrono::seconds>(now - d_last_log).count();
    if (dt >= 5) {
        auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                              now - d_t0).count();
        // Top-3 weak positions for the log.
        int top3[3] = {-1, -1, -1};
        int top3v[3] = {0, 0, 0};
        for (int i = 0; i < CODE_LEN; i++) {
            int v = d_hist_pos[i];
            if (v > top3v[0]) {
                top3v[2] = top3v[1]; top3[2] = top3[1];
                top3v[1] = top3v[0]; top3[1] = top3[0];
                top3v[0] = v;        top3[0] = i;
            } else if (v > top3v[1]) {
                top3v[2] = top3v[1]; top3[2] = top3[1];
                top3v[1] = v;        top3[1] = i;
            } else if (v > top3v[2]) {
                top3v[2] = v;        top3[2] = i;
            }
        }
        std::fprintf(stderr,
                     "[rs_erasure t=%6.1fs] pkts=%d ec=%d era_dec=%d "
                     "era_ok=%d bad=%d "
                     "(last5s: pkts=%d era_dec=%d era_ok=%d bad=%d)  "
                     "weak_pos[%d:%d,%d:%d,%d:%d]\n",
                     elapsed_ms / 1000.0,
                     d_packets, d_errors_corrected,
                     d_erasure_decodes, d_erasure_successes, d_bad_packets,
                     d_log_packets, d_log_eras_dec, d_log_eras_ok, d_log_bad,
                     top3[0], top3v[0], top3[1], top3v[1], top3[2], top3v[2]);
        d_last_log = now;
        d_log_packets  = 0;
        d_log_eras_dec = 0;
        d_log_eras_ok  = 0;
        d_log_bad      = 0;
    }

    return noutput_items;
}

} /* namespace atscplus */
} /* namespace gr */
