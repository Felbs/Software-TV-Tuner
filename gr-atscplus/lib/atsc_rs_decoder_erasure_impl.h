/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */

#ifndef INCLUDED_ATSCPLUS_ATSC_RS_DECODER_ERASURE_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_RS_DECODER_ERASURE_IMPL_H

#include <gnuradio/atscplus/atsc_rs_decoder_erasure.h>
#include <gnuradio/dtv/atsc_plinfo.h>
#include <array>
#include <chrono>
#include <cstdint>
#include <deque>
#include <string>
#include <vector>

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
    // SICKMAP (turbo stage-2a, 2026-07-07 night): cross-codeword erasure
    // targeting. Corrections observed in DECODED codewords map back through
    // the interleaver to transmission-time segments; failed codewords boost
    // erasure priority for bytes that came from those sick moments. The
    // outer code teaching itself through the interleaver's geometry.
    uint64_t d_deint_anchor = 0;   // abs item idx of commutator-phase zero
    bool d_anchor_seen = false;
    int64_t d_n_since = -1;        // this codeword's index in anchor phase
    uint8_t d_sick[512] = {};      // ring over transmission-time segments
    long d_sick_wr = 0;            // corrections recorded into the map
    long d_sick_hit = 0;           // failed-codeword bytes boosted by it

    // byte pos of codeword d_n_since -> transmission-time segment (or -1).
    // branch b = (pos - n) mod 52; lag = 156 + 208*(51-b) bytes (156 = 3*52
    // cancels out of the phase, verified against plinfo's 52-segment delay)
    inline int64_t tx_seg(int pos) const
    {
        if (d_n_since < 0)
            return -1;
        const int b = (int)(((pos - (d_n_since % 52)) % 52 + 52) % 52);
        const int64_t P = d_n_since * 207 + pos;
        const int64_t T = P - (156 + 208 * (51 - b));
        return T < 0 ? -1 : T / 207;
    }
    inline void sick_mark(int pos)
    {
        const int64_t T = tx_seg(pos);
        if (T >= 0) {
            uint8_t& s = d_sick[T & 511];
            if (s < 250)
                s++;
            d_sick_wr++;
        }
    }

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
    // corr207 (optional): receives the full corrected 207-byte codeword on
    // success (data + parity) — the turbo stage-2b pinning source.
    int decode_block(const unsigned char* in207, unsigned char* out188,
                 const unsigned char* rel207 = nullptr,
                 unsigned char* corr207 = nullptr);

    // ── TURBO STAGE 2B (2026-07-10): trellis pinning ──────────────────
    // RS-truth back-propagation per TURBO_BLUEPRINT.md stage 2. When a
    // codeword fails RS+GMD, the bytes RS *did* decode in codewords that
    // share its interleaver span are mapped back to trellis coordinates
    // and PINNED (branch-metric prior) in a second Viterbi pass over the
    // buffered soft symbols; the re-decoded bytes get one more RS+GMD
    // attempt. Opt-in via STVT_TURBO=1; requires optional input port 3 =
    // post-equalizer soft symbols (float x 832, same item index space —
    // every block in the chain is a sync block). Default OFF: the work()
    // path is byte-identical to the pre-turbo build.
    static constexpr int SEG_FLOATS = 832;         // symbols per segment
    static constexpr int TSOFT_RING = 256;         // soft ring (segments, pow2)
    static constexpr int TKNOWN_RING = 256;        // known ring (codewords, pow2)
    struct turbo_known {
        uint64_t abs = ~0ull;
        uint8_t ok = 0;
        unsigned char cw[CODE_LEN];
    };
    struct turbo_pending {
        uint64_t abs;
        gr::dtv::plinfo pl;
        int rs;                 // decode_block result; -2 = non-regular
        bool regular;
        bool has_rel;
        unsigned char in207[CODE_LEN];
        unsigned char rel207[CODE_LEN];
        unsigned char out188[PKT_LEN];
    };
    bool d_turbo = false;          // STVT_TURBO=1 master switch
    bool d_turbo_checked = false;  // first-work port presence check done
    int d_turbo_lag = 56;          // codewords of finalization delay (>52)
    int d_turbo_bytes = 64;        // weakest-SOVA bytes re-decoded per failure
    int d_turbo_ctx = 48;          // pinned-viterbi context symbols per side
    int d_turbo_pinz2 = 1;         // resolve Z2 chains through the postcoder
    int d_turbo_selftest = 0;      // no-pin re-decode agreement check only
    long d_turbo_maxsym = 250000;  // per-rescue symbol budget
    // stampede gate (2026-07-10 RF15 lesson): on a >few-% failure-rate
    // channel turbo's re-decodes blow the real-time budget (768k
    // attempts/2min -> source overflows -> more failures). Above the
    // gate turbo converts ~0% anyway (bimodal law), so stand down.
    double d_turbo_failema = 0.0;      // EMA of failure indicator
    double d_turbo_maxfail = 0.04;     // gate: skip rescues above this
    std::vector<float> d_soft_ring;      // TSOFT_RING * SEG_FLOATS
    std::vector<uint64_t> d_soft_abs;    // slot -> abs segment index
    std::vector<turbo_known> d_known;
    std::deque<turbo_pending> d_pending;
    long d_turbo_att = 0;      // failed codewords entering rescue
    long d_turbo_retry = 0;    // rescues that changed >=1 byte (RS retried)
    long d_turbo_resc = 0;     // rescues where the RS retry succeeded
    long d_turbo_repl = 0;     // total bytes replaced by pinned re-decode
    long d_turbo_syms = 0;     // total symbols re-decoded
    long d_turbo_skip = 0;     // clusters skipped (ring miss / budget)
    long d_turbo_selfok = 0;   // selftest: re-decoded bytes matching pass 1
    long d_turbo_selftot = 0;
    // attempt pinned re-decode + RS retry on finalization of a failure
    bool turbo_rescue(turbo_pending& F);

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
