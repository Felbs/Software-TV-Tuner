/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */
/* See atsc_rs_decoder_erasure.h for the algorithmic rationale. */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_rs_decoder_erasure_impl.h"
#include <gnuradio/io_signature.h>
#include <gnuradio/dtv/atsc_plinfo.h>
// gnuradio/fec/rs.h is a C header; gr-fec was built without extern "C"
// guards in its public header. Wrap it here so the symbols resolve.
extern "C" {
#include <gnuradio/fec/rs.h>
}

#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

namespace gr {
namespace atscplus {

atsc_rs_decoder_erasure::sptr atsc_rs_decoder_erasure::make(int max_erasures)
{
    return gnuradio::make_block_sptr<atsc_rs_decoder_erasure_impl>(max_erasures);
}

atsc_rs_decoder_erasure_impl::atsc_rs_decoder_erasure_impl(int max_erasures)
    : sync_block("atscplus_atsc_rs_decoder_erasure",
                 // 2026-05-21 Day 4 fix: match stock dtv.atsc_rs_decoder
                 // interface — 2 input ports (data + plinfo), 2 output
                 // ports. plinfo lets us skip field-sync segments which
                 // aren't RS-encoded and would produce 100% garbage if
                 // decoded as data. THIS WAS THE PSI-CORRUPTION BUG.
                 io_signature::make2(2, 2,
                     sizeof(unsigned char) * CODE_LEN,
                     sizeof(gr::dtv::plinfo)),
                 io_signature::make2(2, 2,
                     sizeof(unsigned char) * PKT_LEN,
                     sizeof(gr::dtv::plinfo)))
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
    d_miscorrections     = 0;
    d_bad_packets        = 0;
    d_recent_metric      = 0.0;
    d_recent_metric_max  = 0.0;
    d_metric_tag_count   = 0;
    d_effective_max_erasures = d_max_erasures;

    d_t0       = std::chrono::steady_clock::now();
    d_last_log = d_t0;
    d_log_packets  = 0;
    d_log_eras_dec = 0;
    d_log_eras_ok  = 0;
    d_log_bad      = 0;

    // Day 10 (2026-05-21): persistent histogram. Saves the empirical
    // weak-position histogram to a file on shutdown and reloads it on
    // startup so erasure retry kicks in from segment 1 instead of after
    // the ~60-120s cold-start required to populate from scratch.
    // Disable by setting env STVT_RS_HIST_FILE=/dev/null.
    d_hist_path = "/tmp/atscplus_rs_erasure_hist.bin";
    if (const char* p = std::getenv("STVT_RS_HIST_FILE"))
        d_hist_path = p;
    d_save_period_packets = 30000;   // ~2.5s at 12k segments/sec
    d_packets_since_save  = 0;
    load_histogram();

    std::fprintf(stderr,
                 "[rs_erasure] init max_erasures=%d decay_period=%d hist_count=%d\n",
                 d_max_erasures, d_hist_decay_period, d_hist_count);
}

void atsc_rs_decoder_erasure_impl::load_histogram()
{
    if (d_hist_path == "/dev/null") {
        std::fprintf(stderr, "[rs_erasure] hist load: disabled (/dev/null)\n");
        return;
    }
    std::FILE* f = std::fopen(d_hist_path.c_str(), "rb");
    if (!f) {
        std::fprintf(stderr, "[rs_erasure] hist load: fopen(%s) failed errno=%d\n",
                     d_hist_path.c_str(), errno);
        return;
    }
    uint32_t magic = 0;
    uint32_t version = 0;
    uint32_t code_len = 0;
    uint32_t hist_count = 0;
    size_t r = std::fread(&magic, sizeof(magic), 1, f);
    if (r != 1 || magic != HIST_MAGIC) {
        std::fprintf(stderr,
            "[rs_erasure] hist load: bad magic (read %zu items, magic=0x%08x expected 0x%08x)\n",
            r, magic, (uint32_t)HIST_MAGIC);
        std::fclose(f); return;
    }
    r = std::fread(&version, sizeof(version), 1, f);
    if (r != 1 || version != HIST_VERSION) {
        std::fprintf(stderr,
            "[rs_erasure] hist load: bad version (read %zu, ver=%u expected %u)\n",
            r, version, (uint32_t)HIST_VERSION);
        std::fclose(f); return;
    }
    r = std::fread(&code_len, sizeof(code_len), 1, f);
    if (r != 1 || code_len != (uint32_t)CODE_LEN) {
        std::fprintf(stderr,
            "[rs_erasure] hist load: bad code_len (read %zu, code_len=%u expected %u)\n",
            r, code_len, (uint32_t)CODE_LEN);
        std::fclose(f); return;
    }
    r = std::fread(&hist_count, sizeof(hist_count), 1, f);
    if (r != 1) {
        std::fprintf(stderr, "[rs_erasure] hist load: fread hist_count failed (r=%zu)\n", r);
        std::fclose(f); return;
    }
    int32_t pos[CODE_LEN];
    r = std::fread(pos, sizeof(int32_t), CODE_LEN, f);
    if (r != (size_t)CODE_LEN) {
        std::fprintf(stderr,
            "[rs_erasure] hist load: fread pos failed (r=%zu, expected %d)\n",
            r, CODE_LEN);
        std::fclose(f); return;
    }
    for (int i = 0; i < CODE_LEN; i++) d_hist_pos[i] = pos[i];
    d_hist_count = static_cast<int>(hist_count);
    std::fclose(f);
    std::fprintf(stderr,
                 "[rs_erasure] loaded histogram from %s hist_count=%d\n",
                 d_hist_path.c_str(), d_hist_count);
}

void atsc_rs_decoder_erasure_impl::save_histogram() const
{
    if (d_hist_path == "/dev/null") return;
    std::string tmp = d_hist_path + ".tmp";
    std::FILE* f = std::fopen(tmp.c_str(), "wb");
    if (!f) return;
    uint32_t magic = HIST_MAGIC;
    uint32_t version = HIST_VERSION;
    uint32_t code_len = CODE_LEN;
    uint32_t hist_count = static_cast<uint32_t>(d_hist_count);
    std::fwrite(&magic, sizeof(magic), 1, f);
    std::fwrite(&version, sizeof(version), 1, f);
    std::fwrite(&code_len, sizeof(code_len), 1, f);
    std::fwrite(&hist_count, sizeof(hist_count), 1, f);
    int32_t pos[CODE_LEN];
    for (int i = 0; i < CODE_LEN; i++) pos[i] = d_hist_pos[i];
    std::fwrite(pos, sizeof(int32_t), CODE_LEN, f);
    std::fclose(f);
    std::rename(tmp.c_str(), d_hist_path.c_str());
}

atsc_rs_decoder_erasure_impl::~atsc_rs_decoder_erasure_impl()
{
    save_histogram();
    if (d_rs)
        free_rs_char(d_rs);
}

// Day 3 (2026-05-21): gate erasure-retry budget on viterbi_metric.
// best_state_metric() in atsc_single_viterbi_soft grows with path
// uncertainty. Observed range in production: 3000-7000, mean ~5000.
// Lower = more confident signal — fewer erasures needed (saves CPU
// when chain is clean). Higher = noisier — push erasures up to recover
// more packets. Returns clamped to [1, 20] (RS code allows up to 20).
// 2026-05-27: now uses the WORSE of (avg, max) for the gate. The avg
// across 12 decoders dilutes the case where a single decoder is broken
// (1 broken / 11 healthy → avg barely moves, but the broken decoder's
// dibits become a fixed-pattern corruption that RS must erase to fix).
// Using max(avg, max/2) keeps the avg-driven baseline behaviour for
// uniform noise but kicks the budget up when one decoder is hosed.
int atsc_rs_decoder_erasure_impl::dynamic_max_erasures() const
{
    if (d_metric_tag_count == 0) return d_max_erasures;   // no signal yet
    const double m = std::max(d_recent_metric, d_recent_metric_max * 0.5);
    if (m < 3500.0) return std::max(4, d_max_erasures - 6);
    if (m < 5500.0) return d_max_erasures;
    // ceiling 16, not 20 (2026-07-06): RS allows 2t+s<=20, so s=20 leaves
    // ZERO error margin — any stray error can satisfy parity on a WRONG
    // codeword. That zero-margin mode measured 30k miscorrections/145s
    // on a borderline signal. s<=16 keeps a 2-error cushion always.
    return std::min(16, d_max_erasures + 4);
}

int atsc_rs_decoder_erasure_impl::decode_block(const unsigned char* in207,
                                               unsigned char* out188)
{
    // (207, 187) shortened from (255, 235) — 48 byte implicit zero pad
    // at the front of the 255-byte buffer fed to decode_rs_char.
    // Stock dtv.atsc_rs_decoder copies 188 bytes from tmp[PAD..PAD+188]
    // (which is the first 188 bytes of the 207-byte codeword: sync byte
    // at offset 0 already in place because deinterleaver preserves it).
    // Day 6: we previously forced out[0]=0x47 and shifted bytes by 1 —
    // that DUPLICATED the sync byte at offset 1 (the PID-high byte),
    // breaking ALL PID parsing including PSI PAT/PMT. THAT was the bug.
    unsigned char tmp[RS_N];
    std::memset(tmp, 0, PAD_BYTES);
    std::memcpy(tmp + PAD_BYTES, in207, CODE_LEN);

    // First attempt: hard decode (no erasures).
    int n = decode_rs_char(d_rs, tmp, nullptr, 0);
    if (n >= 0) {
        // guard v2 witness: hard decodes are ground truth for live PIDs
        {
            const uint16_t pid =
                ((tmp[PAD_BYTES + 1] & 0x1F) << 8) | tmp[PAD_BYTES + 2];
            if (d_pid_seen[pid] < 60000) d_pid_seen[pid]++;
            if (++d_pid_seen_total % 500000 == 0)      // slow aging
                for (auto& c : d_pid_seen) c >>= 1;
        }
        // Update histogram with positions that were corrected.
        // Compare corrected tmp[PAD..PAD+CODE_LEN] against original in207.
        for (int i = 0; i < CODE_LEN; i++) {
            if (tmp[PAD_BYTES + i] != in207[i]) {
                int v = d_hist_pos[i] + 1;
                if (v > 2000) v = 2000;
                d_hist_pos[i] = v;
            }
        }
        std::memcpy(out188, tmp + PAD_BYTES, PKT_LEN);
        d_hist_count++;
        return n;
    }

    // First attempt failed.
    // If we don't have meaningful histogram yet, give up.
    if (d_hist_count < 20) {
        std::memcpy(out188, in207, PKT_LEN);
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
        std::memcpy(out188, in207, PKT_LEN);
        return -1;
    }
    std::sort(ranked.begin(), ranked.end(),
              [](const std::pair<int,int>& a, const std::pair<int,int>& b) {
                  return a.first > b.first;
              });

    int no_eras = std::min<int>({d_effective_max_erasures, (int)ranked.size(), 16});
    int eras_pos[20];
    for (int i = 0; i < no_eras; i++)
        eras_pos[i] = ranked[i].second + PAD_BYTES;   // positions in 255-buf

    // Re-fill tmp because decode_rs_char may have modified it.
    std::memset(tmp, 0, PAD_BYTES);
    std::memcpy(tmp + PAD_BYTES, in207, CODE_LEN);

    d_erasure_decodes++;
    d_log_eras_dec++;
    n = decode_rs_char(d_rs, tmp, eras_pos, no_eras);

    // MISCORRECTION GUARD (2026-05-31). RS(207,187) with up to 20 erasures can
    // satisfy all parity for a WRONG codeword when the true errors are NOT at
    // the guessed histogram positions: decode_rs_char returns n>=0 ("success")
    // but the packet is garbage with a random PID. Emitting those is the entire
    // clean->noise "drift" (proven by deterministic replay — see memory
    // drift_is_erasure_rs_miscorrection). Byte 0 of the codeword is the TS sync
    // (0x47): RS-protected and known-constant, so a decoded sync != 0x47 is a
    // definite miscorrection (catches ~255/256 of them). Reject it — treat as
    // uncorrectable so teiscrub NULLs it (brief freeze) instead of showing
    // noise. The first (hard) decode is unchanged, so clean packets are still
    // byte-identical to stock. STVT_RS_MISCORR_GUARD=0 restores old behavior.
    static const bool MISCORR_GUARD = []() {
        const char* p = std::getenv("STVT_RS_MISCORR_GUARD");
        return !(p && p[0] == '0');
    }();
    bool miscorrect =
        (n >= 0) && MISCORR_GUARD && (tmp[PAD_BYTES] != 0x47);

    // GUARD v2 (2026-07-06): the sync-byte check catches 255/256 wrong
    // codewords; the ~1/256 that leak measured ~0.8 corrupt pkts/s on a
    // borderline signal (2026-07-06 morning A/B) — the visible glitches.
    // Three independent TS invariants multiply the rejection power:
    //   PID witnessed on a hard decode  (wrong codewords have random PIDs)
    //   TEI bit must be 0               (a "corrected" packet can't be errored)
    //   adaptation_field_control != 0   (reserved value never transmitted)
    // STVT_RS_GUARD2=0 disables (A/B hook).
    static const bool GUARD2 = []() {
        const char* p = std::getenv("STVT_RS_GUARD2");
        return !(p && p[0] == '0');
    }();
    if (n >= 0 && !miscorrect && GUARD2 && MISCORR_GUARD) {
        const uint16_t pid =
            ((tmp[PAD_BYTES + 1] & 0x1F) << 8) | tmp[PAD_BYTES + 2];
        const bool tei = (tmp[PAD_BYTES + 1] & 0x80) != 0;
        const int afc = (tmp[PAD_BYTES + 3] >> 4) & 0x3;
        if (tei || afc == 0 ||
            (d_pid_seen_total > 200 && d_pid_seen[pid] == 0)) {
            miscorrect = true;
            d_guard2_rejects++;
        }
    }

    if (n >= 0 && !miscorrect) {
        d_erasure_successes++;
        d_log_eras_ok++;
        // Update histogram ONLY for validated erasure decodes (poisoning the
        // histogram from miscorrections was a positive-feedback loop that
        // sustained the drift). Positions actually-corrected vs in207.
        for (int i = 0; i < CODE_LEN; i++) {
            if (tmp[PAD_BYTES + i] != in207[i]) {
                int v = d_hist_pos[i] + 1;
                if (v > 2000) v = 2000;
                d_hist_pos[i] = v;
            }
        }
        std::memcpy(out188, tmp + PAD_BYTES, PKT_LEN);
        d_hist_count++;
        return n;
    }

    if (miscorrect) d_miscorrections++;
    // Uncorrectable (or rejected miscorrection) — return data as-is, mark TEI.
    std::memcpy(out188, in207, PKT_LEN);
    return -1;
}

int atsc_rs_decoder_erasure_impl::work(int noutput_items,
                                       gr_vector_const_void_star& input_items,
                                       gr_vector_void_star&       output_items)
{
    auto in     = static_cast<const unsigned char*>(input_items[0]);
    auto plin   = static_cast<const gr::dtv::plinfo*>(input_items[1]);
    auto out    = static_cast<unsigned char*>(output_items[0]);
    auto plout  = static_cast<gr::dtv::plinfo*>(output_items[1]);

    (void)0;  // Day 6: removed data187[] — decode_block writes directly to out_pkt

    // 2026-05-21 Day 2: read viterbi_metric tags emitted by
    // atscplus.atsc_viterbi_soft (12 segments per tag, propagated through
    // the deinterleaver as a stream tag — values are off by the
    // deinterleaver's 52-segment delay but the running mean still tracks
    // RF SNR). Tags only present when chain uses STVT_VITERBI=soft.
    std::vector<tag_t> tags;
    get_tags_in_window(tags, 0, 0, noutput_items,
                       pmt::intern("viterbi_metric"));
    for (const auto& t : tags) {
        if (pmt::is_real(t.value) || pmt::is_integer(t.value)) {
            d_recent_metric = pmt::to_double(t.value);
            d_metric_tag_count++;
        }
    }
    // 2026-05-27: also consume the worst-decoder metric for budget gating.
    std::vector<tag_t> tags_max;
    get_tags_in_window(tags_max, 0, 0, noutput_items,
                       pmt::intern("viterbi_metric_max"));
    for (const auto& t : tags_max) {
        if (pmt::is_real(t.value) || pmt::is_integer(t.value)) {
            d_recent_metric_max = pmt::to_double(t.value);
        }
    }
    // Day 3: update effective erasure budget from latest metric.
    d_effective_max_erasures = dynamic_max_erasures();

    for (int i = 0; i < noutput_items; i++) {
        const unsigned char* in_pkt  = in  + i * CODE_LEN;
        unsigned char*       out_pkt = out + i * PKT_LEN;

        // Always propagate plinfo (downstream blocks need it).
        plout[i] = plin[i];

        // Skip RS decode on non-regular (field-sync) segments — they
        // aren't RS-encoded data. Day 5: stock RS probably copies the
        // input bytes through unchanged (NOT a fake NULL packet — that
        // confuses downstream derand whose PN-sync depends on input
        // content matching field-sync patterns). Copy first 188 bytes
        // of the 207-byte codeword through. plinfo tells depad downstream
        // to strip the segment.
        if (!plin[i].regular_seg_p()) {
            std::memcpy(out_pkt, in_pkt, PKT_LEN);
            d_packets++;
            d_log_packets++;
            continue;
        }

        int n = decode_block(in_pkt, out_pkt);
        d_packets++;
        d_log_packets++;

        // Day 6: ALSO set plinfo.transport_error so downstream
        // (depad/derand) sees the same authoritative TEI flag stock RS
        // uses. Don't overwrite byte 1 of TS — sync is at byte 0 already.
        plout[i].set_transport_error(n == -1);

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

    // Day 10: periodically checkpoint histogram to disk so next chain
    // run skips the 60-120s cold-start.
    d_packets_since_save += noutput_items;
    if (d_packets_since_save >= d_save_period_packets) {
        save_histogram();
        d_packets_since_save = 0;
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
                     "era_ok=%d miscorr=%d bad=%d "
                     "(last5s: pkts=%d era_dec=%d era_ok=%d bad=%d)  "
                     "weak_pos[%d:%d,%d:%d,%d:%d]  "
                     "vit_metric=%.3f vit_max=%.3f tags=%d eff_eras=%d "
                     "g2rej=%d\n",
                     elapsed_ms / 1000.0,
                     d_packets, d_errors_corrected,
                     d_erasure_decodes, d_erasure_successes, d_miscorrections,
                     d_bad_packets,
                     d_log_packets, d_log_eras_dec, d_log_eras_ok, d_log_bad,
                     top3[0], top3v[0], top3[1], top3v[1], top3[2], top3v[2],
                     d_recent_metric, d_recent_metric_max,
                     d_metric_tag_count, d_effective_max_erasures,
                     d_guard2_rejects);
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
