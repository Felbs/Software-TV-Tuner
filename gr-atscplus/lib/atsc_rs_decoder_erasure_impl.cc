/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */
/* See atsc_rs_decoder_erasure.h for the algorithmic rationale. */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_rs_decoder_erasure_impl.h"
#include <gnuradio/io_signature.h>
#include <gnuradio/dtv/atsc_plinfo.h>
// Trellis mux geometry tables (enco_which_syms / enco_which_dibits) shared
// with atsc_viterbi_soft — the turbo stage-2b back-mapper needs them.
#include "atsc_viterbi_mux.h"
// gnuradio/fec/rs.h is a C header; gr-fec was built without extern "C"
// guards in its public header. Wrap it here so the symbols resolve.
extern "C" {
#include <gnuradio/fec/rs.h>
}

#include <algorithm>
#include <cerrno>
#include <cmath>
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
                 // input 2 (optional, 2026-07-07): SOVA per-byte
                 // reliability plane from the viterbi (deinterleaved by
                 // a twin deinterleaver) — TRUE erasure positions
                 // input 3 (optional, 2026-07-10 turbo stage-2b): the
                 // post-equalizer SOFT SYMBOLS (float x 832) — the exact
                 // viterbi input, same item index space (all sync blocks),
                 // buffered here for the pinned second Viterbi pass
                 io_signature::makev(2, 4,
                     std::vector<int>{
                         (int)(sizeof(unsigned char) * CODE_LEN),
                         (int)sizeof(gr::dtv::plinfo),
                         (int)(sizeof(unsigned char) * CODE_LEN),
                         (int)(sizeof(float) * SEG_FLOATS)}),
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

    // ── TURBO STAGE 2B (2026-07-10) — opt-in, default OFF ─────────────
    {
        const char* p = std::getenv("STVT_TURBO");
        d_turbo = p && p[0] == '1';
    }
    if (d_turbo) {
        auto env_int = [](const char* name, long defv, long lo, long hi) {
            if (const char* p = std::getenv(name)) {
                long v = std::atol(p);
                if (v >= lo && v <= hi) return v;
            }
            return defv;
        };
        d_turbo_lag      = (int)env_int("STVT_TURBO_LAG", 56, 53, 200);
        d_turbo_bytes    = (int)env_int("STVT_TURBO_BYTES", 64, 1, 207);
        d_turbo_ctx      = (int)env_int("STVT_TURBO_CTX", 48, 8, 400);
        d_turbo_pinz2    = (int)env_int("STVT_TURBO_PINZ2", 1, 0, 1);
        d_turbo_selftest = (int)env_int("STVT_TURBO_SELFTEST", 0, 0, 1);
        d_turbo_maxsym   = env_int("STVT_TURBO_MAXSYM", 250000, 1000, 100000000);
        d_soft_ring.assign((size_t)TSOFT_RING * SEG_FLOATS, 0.0f);
        d_soft_abs.assign(TSOFT_RING, ~0ull);
        d_known.assign(TKNOWN_RING, turbo_known());
        std::fprintf(stderr,
                     "[rs_erasure] TURBO 2B armed: lag=%d bytes=%d ctx=%d "
                     "pinz2=%d selftest=%d maxsym=%ld\n",
                     d_turbo_lag, d_turbo_bytes, d_turbo_ctx, d_turbo_pinz2,
                     d_turbo_selftest, d_turbo_maxsym);
    }
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
                                               unsigned char* out188,
                                               const unsigned char* rel207,
                                               unsigned char* corr207)
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

    // 2026-07-06 DEAF forensics: is the INPUT still framed? Sync byte
    // 0x47 present pre-decode = deinterleaver alignment survived; absent
    // = the commutator slipped (persistent-state suspect). Printed as
    // sync5s in the telemetry line.
    if (in207[0] == 0x47) d_log_syncok++;

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
                sick_mark(i);      // SICKMAP writer: correction -> tx time
            }
        }
        std::memcpy(out188, tmp + PAD_BYTES, PKT_LEN);
        if (corr207)
            std::memcpy(corr207, tmp + PAD_BYTES, CODE_LEN);
        d_hist_count++;
        return n;
    }

    // First attempt failed.
    int no_eras = 0;
    int eras_pos[20];
    if (rel207) {
        // ── GMD / FORNEY DECODING (2026-07-07 night) ── the canonical
        // iterative decoding of concatenated codes, powered by SOVA
        // reliabilities: walk the erasure ladder (2, 4, ... 16 weakest
        // bytes) and take the FIRST solution that survives the full
        // guard battery. One fixed pattern wastes correction power
        // erasing good bytes; GMD finds each codeword's sweet size.
        // SICKMAP reader (STVT_RS_SICKMAP=1 enables): bytes whose
        // transmission moment produced corrections in OTHER codewords get
        // their weakness boosted — the interleaver map aims the erasures.
        // OPT-IN after 7/07 replay A/Bs: wash on canyon AND impulse —
        // failure is bimodal (lightly wounded or annihilated); the
        // erasure-rescue pool is ~0.06% regardless of aim. Writer +
        // telemetry stay always-on as a live disease map.
        static const bool SICKMAP = []() {
            const char* p = std::getenv("STVT_RS_SICKMAP");
            return p && p[0] == '1';
        }();
        std::vector<std::pair<int, int>> weak;   // (eff weakness, pos)
        weak.reserve(CODE_LEN);
        for (int i = 0; i < CODE_LEN; i++) {
            int key = rel207[i];
            if (SICKMAP) {
                const int64_t T = tx_seg(i);
                if (T >= 0) {
                    const int s = d_sick[T & 511];
                    if (s) {
                        key -= 96 * (s > 2 ? 2 : s);
                        d_sick_hit++;
                    }
                }
            }
            weak.emplace_back(key, i);
        }
        std::sort(weak.begin(), weak.end());
        const int smax = std::min(d_effective_max_erasures, 16);
        // STVT_RS_GMD=0 restores the single fixed-smax attempt (A/B hook)
        static const bool GMD = []() {
            const char* p = std::getenv("STVT_RS_GMD");
            return !(p && p[0] == '0');
        }();
        const int sstart = GMD ? 2 : smax;
        // one "erasure decode" per ladder walk (keeps era_dec's meaning);
        // per-trial counts go to gmd_trials/gmd_rej
        d_erasure_decodes++;
        d_log_eras_dec++;
        for (int s = sstart; s <= smax; s += 2) {
            for (int i = 0; i < s; i++)
                eras_pos[i] = weak[i].second + PAD_BYTES;
            std::memset(tmp, 0, PAD_BYTES);
            std::memcpy(tmp + PAD_BYTES, in207, CODE_LEN);
            d_gmd_trials++;
            int ng = decode_rs_char(d_rs, tmp, eras_pos, s);
            if (ng < 0)
                continue;
            // guard battery per trial: sync byte + TS invariants + PID
            if (tmp[PAD_BYTES] != 0x47) {
                d_gmd_rej++;
                continue;
            }
            const uint16_t pid =
                ((tmp[PAD_BYTES + 1] & 0x1F) << 8) | tmp[PAD_BYTES + 2];
            const bool tei = (tmp[PAD_BYTES + 1] & 0x80) != 0;
            const int afc = (tmp[PAD_BYTES + 3] >> 4) & 0x3;
            if (tei || afc == 0 ||
                (d_pid_seen_total > 200 && d_pid_seen[pid] == 0)) {
                d_gmd_rej++;
                continue;
            }
            // accepted: a GMD rescue
            d_erasure_successes++;
            d_log_eras_ok++;
            for (int i = 0; i < CODE_LEN; i++) {
                if (tmp[PAD_BYTES + i] != in207[i]) {
                    int v = d_hist_pos[i] + 1;
                    if (v > 2000) v = 2000;
                    d_hist_pos[i] = v;
                    sick_mark(i);
                }
            }
            std::memcpy(out188, tmp + PAD_BYTES, PKT_LEN);
            if (corr207)
                std::memcpy(corr207, tmp + PAD_BYTES, CODE_LEN);
            d_hist_count++;
            return ng;
        }
        // ladder exhausted — uncorrectable
        std::memcpy(out188, in207, PKT_LEN);
        return -1;
    } else {
        // legacy: empirical position histogram
        if (d_hist_count < 20) {
            std::memcpy(out188, in207, PKT_LEN);
            return -1;
        }
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
        no_eras = std::min<int>({d_effective_max_erasures,
                                 (int)ranked.size(), 16});
        for (int i = 0; i < no_eras; i++)
            eras_pos[i] = ranked[i].second + PAD_BYTES;
    }

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
        if (corr207)
            std::memcpy(corr207, tmp + PAD_BYTES, CODE_LEN);
        d_hist_count++;
        return n;
    }

    if (miscorrect) d_miscorrections++;
    // Uncorrectable (or rejected miscorrection) — return data as-is, mark TEI.
    std::memcpy(out188, in207, PKT_LEN);
    return -1;
}

// ═════════════════ TURBO STAGE 2B machinery (2026-07-10) ═════════════════
// RS-truth back-propagation (TURBO_BLUEPRINT.md stage 2): the convolutional
// interleaver spreads each codeword across 52 segments, so when a codeword
// FAILS, most bytes of the trellis stretches that fed it belong to OTHER
// codewords that DECODED — known-correct symbols. Pin those branches in a
// second Viterbi pass over the buffered soft input and give RS+GMD one more
// look at the re-decoded bytes.
//
// Geometry (verified against tx_seg() and the viterbi fifo arithmetic):
//   deint:   output byte at anchor-rel pos P came from pre-deint byte
//            S = P - 156 - 208*(51 - (P mod 52))        [156 ≡ 0 (mod 52)]
//   viterbi: output byte (seg s, byte j) dibit d  <->  (encoder e, slot k)
//            via enco_which_dibits; its SYMBOL lives one 12-seg batch
//            earlier: fifo(797) + traceback(31) = 828 = one batch per
//            decoder, so slot (t, k) decodes symbol enco_which_syms[e][k]
//            of batch t-1 (abs segment 12*(t-1) + sym/832, sample sym%832).
namespace {

// trellis tables — replicas of atsc_single_viterbi_soft's (kept protected
// there); pair index = (Z2<<1)|Z1, symbol level index = 4*Z2 + 2*Z1 + Z0.
static const int TB_was_sent[4][4] = {
    { 0, 2, 4, 6 }, { 0, 2, 4, 6 }, { 1, 3, 5, 7 }, { 1, 3, 5, 7 }
};
static const int TB_trans[4][4] = {
    { 0, 2, 0, 2 }, { 2, 0, 2, 0 }, { 1, 3, 1, 3 }, { 3, 1, 3, 1 }
};

struct turbo_pin_t {
    uint8_t mode; // 0 = free, 1 = Z1 pinned, 2 = Z1+Z2 pinned
    uint8_t z1;
    uint8_t z2;
    uint8_t x2;   // known postcoder OUTPUT bit (Z2-chain resolution input)
};

// Full-traceback Viterbi over one span with per-symbol branch pins.
// Same L1 branch metric as atsc_single_viterbi_soft; better traceback
// (whole-span, not 32-truncated). pairs_out[i] = (Z2<<1)|Z1 at step i.
static void run_pinned_viterbi(const float* syms,
                               const turbo_pin_t* pins,
                               int L,
                               std::vector<uint8_t>& tb_scratch,
                               uint8_t* pairs_out)
{
    float pm[4] = { 0, 0, 0, 0 };
    tb_scratch.assign((size_t)L * 4, 0);
    for (int i = 0; i < L; i++) {
        float dist[8];
        for (int s = 0; s < 8; s++)
            dist[s] = std::fabs(syms[i] - (float)(2 * s - 7));
        const turbo_pin_t& p = pins[i];
        float npm[4];
        for (int st = 0; st < 4; st++) {
            float best = 1e30f;
            int bp = 0;
            for (int pair = 0; pair < 4; pair++) {
                float m = dist[TB_was_sent[st][pair]] + pm[TB_trans[st][pair]];
                if (p.mode >= 1 && (pair & 1) != p.z1)
                    m += 1.0e6f;
                if (p.mode == 2 && ((pair >> 1) & 1) != p.z2)
                    m += 1.0e6f;
                if (m < best) {
                    best = m;
                    bp = pair;
                }
            }
            npm[st] = best;
            tb_scratch[(size_t)i * 4 + st] = (uint8_t)bp;
        }
        const float mn =
            std::min(std::min(npm[0], npm[1]), std::min(npm[2], npm[3]));
        for (int st = 0; st < 4; st++)
            pm[st] = npm[st] - mn;
    }
    int st = 0;
    float b = pm[0];
    for (int s = 1; s < 4; s++)
        if (pm[s] < b) {
            b = pm[s];
            st = s;
        }
    for (int i = L - 1; i >= 0; i--) {
        const uint8_t pair = tb_scratch[(size_t)i * 4 + st];
        pairs_out[i] = pair;
        st = TB_trans[st][pair];
    }
}

// Inverse of the viterbi mux tables: batch byte index (0..2483) + dibit
// (0..3 = bit-shift/2) -> (encoder, slot k). Built once, 12*828*4 entries
// exactly cover 2484 bytes x 4 dibits.
struct mux_inverse {
    uint8_t enc[12 * 207][4];
    uint16_t kk[12 * 207][4];
    mux_inverse()
    {
        for (int e = 0; e < 12; e++)
            for (unsigned k = 0; k < enco_which_max; k++) {
                const unsigned dbwhere = enco_which_dibits[e][k];
                const unsigned idx = dbwhere >> 3;
                const unsigned d = (dbwhere & 0x7) >> 1;
                enc[idx][d] = (uint8_t)e;
                kk[idx][d] = (uint16_t)k;
            }
    }
};
static const mux_inverse& muxinv()
{
    static const mux_inverse m;
    return m;
}

} // anonymous namespace

bool atsc_rs_decoder_erasure_impl::turbo_rescue(turbo_pending& F)
{
    if (!d_anchor_seen || !F.has_rel)
        return false;
    if (F.abs < d_deint_anchor)
        return false;
    const int64_t n_rel = (int64_t)(F.abs - d_deint_anchor);
    if (n_rel < 53)
        return false; // all 52 source segments must postdate the anchor

    d_turbo_att++;

    // 1) targets: the weakest-SOVA bytes of the failed codeword.
    int order[CODE_LEN];
    for (int j = 0; j < CODE_LEN; j++)
        order[j] = j;
    std::sort(order, order + CODE_LEN, [&](int a, int b2) {
        if (F.rel207[a] != F.rel207[b2])
            return F.rel207[a] < F.rel207[b2];
        return a < b2;
    });
    const int ntar = std::min(d_turbo_bytes, (int)CODE_LEN);

    // 2) back-map targets to per-encoder trellis slot indices
    //    M = t*828 + k (continuous across batches in decode-call order).
    std::vector<int64_t> tgt[12];
    const mux_inverse& inv = muxinv();
    const uint64_t anchor = d_deint_anchor;
    for (int ti = 0; ti < ntar; ti++) {
        const int j = order[ti];
        const int64_t P = n_rel * 207 + j;          // output byte position
        const int b = (int)(P % 52);                // 156 ≡ 0 (mod 52)
        const int64_t S = P - 156 - 208 * (51 - b); // pre-deint byte position
        if (S < 0)
            continue;
        const uint64_t s_abs = anchor + (uint64_t)(S / 207);
        const int j2 = (int)(S % 207);
        const uint64_t t = s_abs / 12;
        if (t < 1)
            continue; // symbols would predate the stream
        const int idx = (int)(s_abs % 12) * 207 + j2;
        for (int d = 0; d < 4; d++) {
            const int e = inv.enc[idx][d];
            tgt[e].push_back((int64_t)t * 828 + inv.kk[idx][d]);
        }
    }

    // candidate re-decoded dibits for bytes of THIS codeword
    uint8_t cand_dib[CODE_LEN][4];
    uint8_t cand_have[CODE_LEN];
    std::memset(cand_have, 0, sizeof(cand_have));

    std::vector<float> syms;
    std::vector<turbo_pin_t> pins;
    std::vector<uint8_t> pairs, tbs;
    long syms_budget = d_turbo_maxsym;

    for (int e = 0; e < 12; e++) {
        auto& v = tgt[e];
        if (v.empty())
            continue;
        std::sort(v.begin(), v.end());
        v.erase(std::unique(v.begin(), v.end()), v.end());
        size_t ci = 0;
        while (ci < v.size()) {
            size_t cj = ci;
            while (cj + 1 < v.size() && v[cj + 1] - v[cj] <= 2 * d_turbo_ctx)
                cj++;
            int64_t M0 = v[ci] - d_turbo_ctx;
            const int64_t M1 = v[cj] + d_turbo_ctx;
            ci = cj + 1;
            if (M0 < 828)
                M0 = 828; // batch >= 1 so the symbol batch (t-1) exists
            const int64_t L = M1 - M0 + 1;
            if (L < 8)
                continue;
            if (L > syms_budget) {
                d_turbo_skip++;
                continue;
            }

            // fetch soft symbols; abort cluster on any ring miss
            syms.resize((size_t)L);
            pins.assign((size_t)L, turbo_pin_t{ 0, 0, 0, 0 });
            bool have_all = true;
            for (int64_t M = M0; M <= M1; M++) {
                const int64_t t = M / 828;
                const int k = (int)(M % 828);
                const unsigned sym = enco_which_syms[e][k];
                const int64_t seg = 12 * (t - 1) + (int64_t)(sym / 832);
                const int slot = (int)(seg & (TSOFT_RING - 1));
                if (seg < 0 || d_soft_abs[slot] != (uint64_t)seg) {
                    have_all = false;
                    break;
                }
                syms[(size_t)(M - M0)] =
                    d_soft_ring[(size_t)slot * SEG_FLOATS + (sym % 832)];
            }
            if (!have_all) {
                d_turbo_skip++;
                continue;
            }

            // pin sources (bytes of DECODED codewords) + suspect registry
            // (bytes of THIS codeword falling in the span)
            struct susp_t {
                int j3;
                int d;
                int64_t M;
            };
            std::vector<susp_t> susp;
            for (int64_t M = M0; M <= M1; M++) {
                const int64_t t = M / 828;
                const int k = (int)(M % 828);
                const unsigned dbwhere = enco_which_dibits[e][k];
                const unsigned idx = dbwhere >> 3;
                const int d = (int)((dbwhere & 0x7) >> 1);
                const uint64_t s_abs = (uint64_t)t * 12 + idx / 207;
                const int j2 = (int)(idx % 207);
                if (s_abs < anchor)
                    continue;
                const int64_t Pp = (int64_t)(s_abs - anchor) * 207 + j2;
                const int b2 = (int)(Pp % 52);
                const int64_t Q = Pp + 208 * (51 - b2) + 156;
                const uint64_t n_out = anchor + (uint64_t)(Q / 207);
                const int j3 = (int)(Q % 207);
                if (n_out == F.abs) {
                    susp.push_back({ j3, d, M });
                    continue;
                }
                if (d_turbo_selftest)
                    continue; // mapping validation runs pin-free
                const turbo_known& K = d_known[n_out & (TKNOWN_RING - 1)];
                if (K.abs != n_out || !K.ok)
                    continue;
                const int vdib = (K.cw[j3] >> (2 * d)) & 0x3;
                turbo_pin_t& pn = pins[(size_t)(M - M0)];
                pn.mode = 1;
                pn.z1 = (uint8_t)(vdib & 1);
                pn.x2 = (uint8_t)((vdib >> 1) & 1);
            }
            if (susp.empty())
                continue;

            // Z2-chain resolution: within each contiguous pinned run the
            // postcoder differential y2[i] = x2[i] ^ y2[i-1] leaves ONE
            // unknown phase bit — score both hypotheses against the
            // received levels and, if decisive, upgrade to a full pin.
            if (d_turbo_pinz2 && !d_turbo_selftest) {
                int64_t i0 = 0;
                while (i0 < L) {
                    if (pins[(size_t)i0].mode == 0) {
                        i0++;
                        continue;
                    }
                    int64_t i1 = i0;
                    while (i1 + 1 < L && pins[(size_t)(i1 + 1)].mode != 0)
                        i1++;
                    float sc[2] = { 0.0f, 0.0f };
                    for (int h = 0; h < 2; h++) {
                        int y2 = h;
                        for (int64_t i = i0; i <= i1; i++) {
                            y2 ^= pins[(size_t)i].x2;
                            const int base = 4 * y2 + 2 * pins[(size_t)i].z1;
                            const float d0 =
                                std::fabs(syms[(size_t)i] - (float)(2 * base - 7));
                            const float d1 =
                                std::fabs(syms[(size_t)i] - (float)(2 * (base + 1) - 7));
                            sc[h] += std::min(d0, d1);
                        }
                    }
                    if (std::fabs(sc[0] - sc[1]) > 1.0f) {
                        int y2 = (sc[0] <= sc[1]) ? 0 : 1;
                        for (int64_t i = i0; i <= i1; i++) {
                            y2 ^= pins[(size_t)i].x2;
                            pins[(size_t)i].mode = 2;
                            pins[(size_t)i].z2 = (uint8_t)y2;
                        }
                    }
                    i0 = i1 + 1;
                }
            }

            pairs.resize((size_t)L);
            run_pinned_viterbi(syms.data(), pins.data(), (int)L, tbs,
                               pairs.data());
            d_turbo_syms += L;
            syms_budget -= L;

            for (const auto& sp : susp) {
                const int64_t i = sp.M - M0;
                if (i < 1)
                    continue; // x2 needs the previous path step
                const int y2 = (pairs[(size_t)i] >> 1) & 1;
                const int y2p = (pairs[(size_t)(i - 1)] >> 1) & 1;
                const int x1 = pairs[(size_t)i] & 1;
                const int x2 = y2 ^ y2p;
                cand_dib[sp.j3][sp.d] = (uint8_t)((x2 << 1) | x1);
                cand_have[sp.j3] |= (uint8_t)(1 << sp.d);
            }
        }
    }

    // 3) assemble replacement bytes and give RS+GMD one more look
    unsigned char new207[CODE_LEN];
    std::memcpy(new207, F.in207, CODE_LEN);
    unsigned char relx[CODE_LEN];
    std::memcpy(relx, F.rel207, CODE_LEN);
    int ndiff = 0;
    for (int j = 0; j < CODE_LEN; j++) {
        if (cand_have[j] != 0x0F)
            continue;
        const unsigned char nb = (unsigned char)(
            (cand_dib[j][3] << 6) | (cand_dib[j][2] << 4) |
            (cand_dib[j][1] << 2) | cand_dib[j][0]);
        if (d_turbo_selftest) {
            d_turbo_selftot++;
            if (nb == F.in207[j])
                d_turbo_selfok++;
            continue;
        }
        if (nb != new207[j]) {
            new207[j] = nb;
            relx[j] = 200; // pinned re-decode: trust above raw SOVA doubt
            ndiff++;
        }
    }
    if (d_turbo_selftest || ndiff == 0)
        return false;
    d_turbo_retry++;
    d_turbo_repl += ndiff;

    unsigned char out188[PKT_LEN];
    unsigned char corr[CODE_LEN];
    const int64_t save_nsince = d_n_since;
    d_n_since = n_rel; // sick_mark attribution for the RETRIED codeword
    const int rs = decode_block(new207, out188, relx, corr);
    d_n_since = save_nsince;
    if (rs < 0)
        return false;

    std::memcpy(F.out188, out188, PKT_LEN);
    F.pl.set_transport_error(false);
    F.rs = rs;
    turbo_known& K = d_known[F.abs & (TKNOWN_RING - 1)];
    K.abs = F.abs;
    K.ok = 1;
    std::memcpy(K.cw, corr, CODE_LEN);
    d_turbo_resc++;
    if (d_bad_packets > 0)
        d_bad_packets--;
    if (d_log_bad > 0)
        d_log_bad--;
    return true;
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

    // SOVA reliability plane (optional input 2): true per-byte doubt
    // from the trellis. When present, erasure positions come from HERE.
    const unsigned char* rel = input_items.size() > 2
        ? static_cast<const unsigned char*>(input_items[2]) : nullptr;

    // TURBO 2B soft-symbol plane (optional input 3): the viterbi's input.
    const float* soft = input_items.size() > 3
        ? static_cast<const float*>(input_items[3]) : nullptr;
    if (d_turbo && !d_turbo_checked) {
        d_turbo_checked = true;
        if (!soft || !rel) {
            std::fprintf(stderr,
                         "[rs_erasure] TURBO requested but %s not connected "
                         "— turbo disabled\n",
                         !soft ? "soft-symbol port (in3)" : "SOVA plane (in2)");
            d_turbo = false;
        }
    }

    // SICKMAP phase anchors: deint_sync tags mark commutator-phase zero.
    // Phase-consistent re-anchors (every field: 312*207 is a multiple of
    // 52) keep the original anchor so the map survives field boundaries;
    // a true resync (glitch) rebases and clears the map.
    std::vector<tag_t> sync_tags;
    get_tags_in_window(sync_tags, 0, 0, noutput_items,
                       pmt::intern("deint_sync"));
    size_t sync_ti = 0;
    const uint64_t abs0 = nitems_read(0);

    for (int i = 0; i < noutput_items; i++) {
        const unsigned char* in_pkt  = in  + i * CODE_LEN;
        const unsigned char* rel_pkt = rel ? rel + i * CODE_LEN : nullptr;
        unsigned char*       out_pkt = out + i * PKT_LEN;

        while (sync_ti < sync_tags.size() &&
               sync_tags[sync_ti].offset <= abs0 + (uint64_t)i) {
            const uint64_t a = sync_tags[sync_ti].offset;
            if (!d_anchor_seen) {
                d_deint_anchor = a;
                d_anchor_seen = true;
                std::memset(d_sick, 0, sizeof(d_sick));
            } else if (((a - d_deint_anchor) % 52) != 0) {
                d_deint_anchor = a;
                std::memset(d_sick, 0, sizeof(d_sick));
            }
            sync_ti++;
        }
        d_n_since = d_anchor_seen
                        ? (int64_t)(abs0 + (uint64_t)i - d_deint_anchor)
                        : -1;
        if (d_n_since >= 0)
            d_sick[d_n_since & 511] = 0;   // expire the slot fresh writes use

        if (!d_turbo) {
            // ═══ ORIGINAL PATH — byte-identical when STVT_TURBO is off ═══
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

            int n = decode_block(in_pkt, out_pkt, rel_pkt);
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
            continue;
        }

        // ═══ TURBO 2B PATH: pass-1 decode into the pending ring, emit
        // with a d_turbo_lag delay so a failed codeword can see the RS
        // outcome of every codeword sharing its interleaver span. ═══
        const uint64_t abs_i = abs0 + (uint64_t)i;

        // capture the soft-symbol segment
        {
            const int slot = (int)(abs_i & (TSOFT_RING - 1));
            std::memcpy(&d_soft_ring[(size_t)slot * SEG_FLOATS],
                        soft + (size_t)i * SEG_FLOATS,
                        SEG_FLOATS * sizeof(float));
            d_soft_abs[slot] = abs_i;
        }

        d_pending.emplace_back();
        turbo_pending& P = d_pending.back();
        P.abs = abs_i;
        P.pl = plin[i];
        P.regular = plin[i].regular_seg_p();
        P.has_rel = rel_pkt != nullptr;
        std::memcpy(P.in207, in_pkt, CODE_LEN);
        if (rel_pkt)
            std::memcpy(P.rel207, rel_pkt, CODE_LEN);
        else
            std::memset(P.rel207, 0, CODE_LEN);

        turbo_known& K = d_known[abs_i & (TKNOWN_RING - 1)];
        K.abs = abs_i;
        K.ok = 0;

        if (!P.regular) {
            std::memcpy(P.out188, in_pkt, PKT_LEN);
            P.rs = -2;
            d_packets++;
            d_log_packets++;
        } else {
            unsigned char corr1[CODE_LEN];
            P.rs = decode_block(in_pkt, P.out188, rel_pkt, corr1);
            d_packets++;
            d_log_packets++;
            P.pl.set_transport_error(P.rs == -1);
            if (P.rs < 0) {
                P.out188[1] |= 0x80;
                d_bad_packets++;
                d_log_bad++;
            } else {
                if (P.rs > 0)
                    d_errors_corrected += P.rs;
                K.ok = 1;
                std::memcpy(K.cw, corr1, CODE_LEN);
            }
        }

        // emit the head of the pending ring (or a warmup null packet)
        if ((int)d_pending.size() > d_turbo_lag) {
            turbo_pending F = d_pending.front();
            d_pending.pop_front();
            if (F.regular && F.rs == -1)
                turbo_rescue(F);
            std::memcpy(out_pkt, F.out188, PKT_LEN);
            plout[i] = F.pl;
        } else {
            // stream warmup: emit a NULL TS packet with default (non-
            // regular) plinfo — depad drops it downstream
            std::memset(out_pkt, 0xFF, PKT_LEN);
            out_pkt[0] = 0x47;
            out_pkt[1] = 0x1F;
            out_pkt[2] = 0xFF;
            out_pkt[3] = 0x10;
            plout[i] = gr::dtv::plinfo();
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
                     "(last5s: pkts=%d era_dec=%d era_ok=%d bad=%d sync=%d)  "
                     "weak_pos[%d:%d,%d:%d,%d:%d]  "
                     "vit_metric=%.3f vit_max=%.3f tags=%d eff_eras=%d "
                     "g2rej=%d gmd_trials=%ld gmd_rej=%d "
                     "sick_wr=%ld sick_hit=%ld\n",
                     elapsed_ms / 1000.0,
                     d_packets, d_errors_corrected,
                     d_erasure_decodes, d_erasure_successes, d_miscorrections,
                     d_bad_packets,
                     d_log_packets, d_log_eras_dec, d_log_eras_ok, d_log_bad,
                     d_log_syncok,
                     top3[0], top3v[0], top3[1], top3v[1], top3[2], top3v[2],
                     d_recent_metric, d_recent_metric_max,
                     d_metric_tag_count, d_effective_max_erasures,
                     d_guard2_rejects, d_gmd_trials, d_gmd_rej,
                     d_sick_wr, d_sick_hit);
        if (d_turbo || d_turbo_att) {
            std::fprintf(stderr,
                         "[rs_turbo t=%6.1fs] att=%ld retry=%ld resc=%ld "
                         "bytes=%ld syms=%ld skip=%ld selftest=%ld/%ld\n",
                         elapsed_ms / 1000.0,
                         d_turbo_att, d_turbo_retry, d_turbo_resc,
                         d_turbo_repl, d_turbo_syms, d_turbo_skip,
                         d_turbo_selfok, d_turbo_selftot);
        }
        d_last_log = now;
        d_log_packets  = 0;
        d_log_eras_dec = 0;
        d_log_eras_ok  = 0;
        d_log_bad      = 0;
        d_log_syncok   = 0;
        d_log_syncok   = 0;
    }

    return noutput_items;
}

} /* namespace atscplus */
} /* namespace gr */
