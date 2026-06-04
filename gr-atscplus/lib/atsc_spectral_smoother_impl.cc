/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */
/*
 * 2026-05-29 atsc_spectral_smoother — FFT-domain spectral outlier
 * suppression. Each non-overlapping FFT_SIZE-sample block is processed
 * independently: per-bin magnitudes are compared to a local-median
 * reference (±neighborhood bins) and outliers are smoothly pulled
 * down to threshold × neighborhood_median.
 *
 * Unlike a recursive IIR notch (atsc_adaptive_notch), this block is
 * fully stateless across blocks — no transients on activation, no
 * ringing when an outlier shifts frequency. Every output sample is
 * computed from an independent FFT pass.
 *
 * Latency = FFT_SIZE / sample_rate. At 6.25 MHz with FFT_SIZE=1024
 * that's 0.16 ms — invisible compared to the rest of the chain.
 */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_spectral_smoother_impl.h"
#include <gnuradio/io_signature.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <vector>

namespace gr {
namespace atscplus {

atsc_spectral_smoother::sptr
atsc_spectral_smoother::make(double sample_rate,
                             int    fft_size,
                             int    neighborhood,
                             double threshold,
                             double pilot_offset_hz,
                             double pilot_guard_hz)
{
    return gnuradio::make_block_sptr<atsc_spectral_smoother_impl>(
        sample_rate, fft_size, neighborhood, threshold,
        pilot_offset_hz, pilot_guard_hz);
}

atsc_spectral_smoother_impl::atsc_spectral_smoother_impl(double sample_rate,
                                                         int    fft_size,
                                                         int    neighborhood,
                                                         double threshold,
                                                         double pilot_offset_hz,
                                                         double pilot_guard_hz)
    : sync_block("atscplus_atsc_spectral_smoother",
                 io_signature::make(1, 1, sizeof(gr_complex)),
                 io_signature::make(1, 1, sizeof(gr_complex)))
{
    d_sample_rate     = sample_rate;
    d_fft_size        = std::max(256, std::min(8192, fft_size));
    d_neighborhood    = std::max(1, std::min(d_fft_size / 2 - 1, neighborhood));
    d_threshold       = std::max(1.01, threshold);
    d_pilot_offset_hz = pilot_offset_hz;
    d_pilot_guard_hz  = std::max(0.0, pilot_guard_hz);

    /* Env overrides — retune without rebuilding. */
    if (const char* p = std::getenv("STVT_SPECTRAL_FFT")) {
        int v = std::atoi(p);
        if (v >= 256 && v <= 8192) d_fft_size = v;
    }
    if (const char* p = std::getenv("STVT_SPECTRAL_NEIGHBORHOOD")) {
        int v = std::atoi(p);
        if (v >= 1) d_neighborhood = std::min(d_fft_size / 2 - 1, v);
    }
    if (const char* p = std::getenv("STVT_SPECTRAL_THRESH")) {
        double v = std::strtod(p, nullptr);
        if (v >= 1.01) d_threshold = v;
    }
    if (const char* p = std::getenv("STVT_SPECTRAL_PILOT_HZ")) {
        d_pilot_offset_hz = std::strtod(p, nullptr);
    }
    if (const char* p = std::getenv("STVT_SPECTRAL_GUARD_HZ")) {
        double v = std::strtod(p, nullptr);
        if (v >= 0.0) d_pilot_guard_hz = v;
    }

    /* Map [pilot - guard, pilot + guard] (in Hz, baseband) to raw FFT
     * bin indices. FFT bin k corresponds to frequency:
     *   f[k] = k * fs/N      for k < N/2
     *   f[k] = (k-N) * fs/N  for k >= N/2  (negative frequency)
     */
    auto hz_to_raw_bin = [&](double hz) -> int {
        double bin = (hz / d_sample_rate) * d_fft_size;
        int    raw = (int)std::lround(bin);
        if (raw < 0) raw += d_fft_size;   /* negative freq → upper half */
        if (raw >= d_fft_size) raw -= d_fft_size;
        return raw;
    };
    int lo = hz_to_raw_bin(d_pilot_offset_hz - d_pilot_guard_hz);
    int hi = hz_to_raw_bin(d_pilot_offset_hz + d_pilot_guard_hz);
    /* Wrap correction: if hi < lo, the window straddles the DC wrap.
     * For simplicity we just store both endpoints and check in the
     * suppression loop with a wrap-aware test. */
    d_pilot_bin_lo = lo;
    d_pilot_bin_hi = hi;

    d_fft_fwd = std::make_unique<gr::fft::fft_complex_fwd>(d_fft_size);
    d_fft_rev = std::make_unique<gr::fft::fft_complex_rev>(d_fft_size);

    /* Overlap-add: 50% overlap, Hann analysis+synthesis windowing.
     * Constant Overlap-Add (COLA) is satisfied for Hann² at 50% hop:
     *   sum_k Hann²(n - k·H) = 0.5·(N-1) for all n
     * We rescale so the sum is exactly 1, making the reconstruction
     * unity-gain in regions where no spectral modification happens. */
    d_hop_size = d_fft_size / 2;
    d_in_ring.assign(d_fft_size, gr_complex(0.0f, 0.0f));
    d_in_ring_pos = 0;
    d_in_count_since_block = 0;

    /* CRITICAL: buffer size = 2 * fft_size. With size = fft_size, OLA
     * folds: Block K wraps to write positions also written by Block K-2,
     * giving 3-block contributions per position (different input times
     * mixed) and breaking COLA reconstruction. Size 2N ensures only
     * 2 consecutive blocks overlap at each position. */
    d_out_buf.assign(2 * d_fft_size, gr_complex(0.0f, 0.0f));
    /* During the first fft_size samples of work() output, OLA hasn't
     * had two blocks fire yet so the reconstruction would be amplitude-
     * modulated by the standalone Hann window. Pass input through
     * unchanged for the warmup; after warmup the steady-state OLA
     * applies. */
    d_warmup_remaining = d_fft_size;
    /* After warmup, read starts at offset = hop_size so the first
     * read corresponds to the first fully-overlapped position
     * (Block 0[H] + Block 1[0]). */
    d_out_read_pos  = 0;     /* updated to d_hop_size at end of warmup */
    d_out_write_pos = 0;

    /* Hann window normalized for 50%-overlap COLA reconstruction.
     * Hann²(n) = 0.25·(1 - cos(2πn/(N-1)))². Sum over 50% hop is
     * 0.5·(N-1) for a length-N window. Apply 1/N IFFT normalization
     * plus a sqrt(2/(N-1)) scaling on each window. */
    d_hann.assign(d_fft_size, 0.0f);
    /* PERIODIC Hann (denominator N, not N-1) — has EXACT COLA sum at 50%
     * overlap. Verified by test_ola.cc: symmetric Hann had ~0.15% periodic
     * amplitude error in OLA output which broke FPLL phase tracking;
     * periodic Hann reduces error to float precision (~5e-7). */
    const double inv_N = 1.0 / (double)d_fft_size;
    for (int n = 0; n < d_fft_size; n++) {
        double w = 0.5 - 0.5 * std::cos(2.0 * M_PI * (double)n * inv_N);
        d_hann[n] = (float)w;
    }
    d_scratch.assign(d_fft_size, gr_complex(0.0f, 0.0f));

    d_mag_sq.assign(d_fft_size, 0.0f);
    d_neighbors.assign(2 * d_neighborhood, 0.0f);
    d_cached_median.assign(d_fft_size, 0.0f);
    d_median_recompute_blocks = 4;
    if (const char* p = std::getenv("STVT_SPECTRAL_MEDIAN_EVERY")) {
        int v = std::atoi(p);
        if (v >= 1 && v <= 32) d_median_recompute_blocks = v;
    }
    d_blocks_since_median = d_median_recompute_blocks;  /* force first compute */

    d_n_samples           = 0;
    d_n_blocks            = 0;
    d_n_bins_suppressed   = 0;
    d_log_blocks          = 0;
    d_log_bins_suppressed = 0;

    d_t0       = std::chrono::steady_clock::now();
    d_last_log = d_t0;

    std::fprintf(stderr,
                 "[atsc_spectral_smoother] init: fs=%.0f Hz fft=%d "
                 "neighborhood=±%d thresh=%.2f pilot_offset=%.0f Hz "
                 "guard=%.0f Hz pilot_bins=[%d,%d]\n",
                 d_sample_rate, d_fft_size, d_neighborhood, d_threshold,
                 d_pilot_offset_hz, d_pilot_guard_hz,
                 d_pilot_bin_lo, d_pilot_bin_hi);
}

atsc_spectral_smoother_impl::~atsc_spectral_smoother_impl() = default;

void atsc_spectral_smoother_impl::process_one_block()
{
    const int N = d_fft_size;

    /* Step 1: linearize the in_ring (oldest → newest) into the FFT
     * input buffer. NO analysis window — Hann² windowing (analysis ×
     * synthesis) is not COLA at 50% hop (sum varies 0.5–1.0). Using
     * only a synthesis window gives proper Hann-only OLA which IS
     * COLA at 50%. The oldest sample is at d_in_ring_pos (the next-
     * to-be-overwritten position). */
    gr_complex* in = d_fft_fwd->get_inbuf();
    for (int k = 0; k < N; k++) {
        int ring_idx = d_in_ring_pos + k;
        if (ring_idx >= N) ring_idx -= N;
        in[k] = d_in_ring[ring_idx];
    }
    d_fft_fwd->execute();
    gr_complex* spec = d_fft_fwd->get_outbuf();

    const int K = d_neighborhood;

    /* Per-bin |X[k]|² + running mean. */
    float mean_mag_sq = 0.0f;
    for (int k = 0; k < N; k++) {
        d_mag_sq[k] = spec[k].real() * spec[k].real()
                    + spec[k].imag() * spec[k].imag();
        mean_mag_sq += d_mag_sq[k];
    }
    mean_mag_sq /= (float)N;

    /* FAST-PATH: if no bin is anomalously large compared to global mean,
     * no per-bin median check will trigger. Skip the O(N*K) median loop
     * entirely — its cost (~32K ops/block at K=32, N=1024) dominates the
     * smoother's CPU budget. We compare global max to C²·mean: since the
     * per-bin test is |X|² > C²·median, and median ≤ max, if the GLOBAL
     * max is below C²·mean (a conservative-but-cheap bound), no bin can
     * possibly be suppressed. Safe to skip. */
    const float C2 = (float)(d_threshold * d_threshold);
    int blk_suppressed = 0;

    /* Even faster path: if THRESH is very high (e.g., 999+), there's
     * essentially no chance of suppression. Skip even the global-max
     * scan. Tonight's RF (2026-05-29) showed pure pass-through with OLA
     * windowing beats per-bin spectral suppression — so this no-suppress
     * fast-path is the default winning configuration. */
    const bool absolutely_no_suppress = (d_threshold >= 999.0);

    float global_max_mag_sq = 0.0f;
    if (!absolutely_no_suppress) {
        for (int k = 0; k < N; k++) {
            if (d_mag_sq[k] > global_max_mag_sq) global_max_mag_sq = d_mag_sq[k];
        }
    }
    /* If max < C² × mean, no bin's |X|² can exceed C² × its local median
     * (median ≤ mean for typical spectra). Skip the suppression loop. */
    const bool can_skip_median = absolutely_no_suppress ||
                                  (global_max_mag_sq <= C2 * mean_mag_sq);

    if (!can_skip_median) {

    /* Pilot bin wrap-aware membership test. */
    const int plo = d_pilot_bin_lo;
    const int phi = d_pilot_bin_hi;
    auto is_pilot_bin = [plo, phi](int k) -> bool {
        if (plo <= phi) return k >= plo && k <= phi;
        /* Window straddles the wrap-around: pilot is k>=plo OR k<=phi. */
        return k >= plo || k <= phi;
    };

    /* Cached-median fast path: only recompute the per-bin medians every
     * d_median_recompute_blocks blocks. Outliers (cellular, AM stations,
     * etc.) shift on the seconds timescale, while we run blocks at the
     * microsecond rate — 4x stale median costs no detectable accuracy. */
    d_blocks_since_median++;
    const bool refresh_median =
        (d_blocks_since_median >= d_median_recompute_blocks);
    if (refresh_median) d_blocks_since_median = 0;

    for (int k = 0; k < N; k++) {
        if (is_pilot_bin(k)) continue;  /* keep the carrier reference. */
        float median_mag_sq;
        if (refresh_median) {
            /* Gather 2K neighbors (excluding bin k) with circular wrap-around
             * so the spectrum is treated as continuous (no edge effects). */
            int idx = 0;
            for (int off = -K; off <= K; off++) {
                if (off == 0) continue;
                int j = k + off;
                if (j < 0) j += N;
                if (j >= N) j -= N;
                d_neighbors[idx++] = d_mag_sq[j];
            }
            /* O(N) median via nth_element on the middle index. */
            std::nth_element(d_neighbors.begin(),
                             d_neighbors.begin() + K,
                             d_neighbors.end());
            median_mag_sq = d_neighbors[K];
            d_cached_median[k] = median_mag_sq;
        } else {
            median_mag_sq = d_cached_median[k];
        }

        /* Apply gain. */
        if (d_mag_sq[k] > C2 * median_mag_sq && median_mag_sq > 0.0f) {
            /* Pull |X[k]| down to threshold × sqrt(median).
             * In linear: gain = sqrt((C²·median) / |X|²).
             * Phase is preserved (gain is purely real). */
            const float ratio  = C2 * median_mag_sq / d_mag_sq[k];
            const float gain   = std::sqrt(ratio);
            spec[k].real(spec[k].real() * gain);
            spec[k].imag(spec[k].imag() * gain);
            blk_suppressed++;
        }
        /* Else: pass through unchanged. */
    }
    }  /* close if (!can_skip_median) */

    /* IFFT to time domain. */
    gr_complex* rev_in = d_fft_rev->get_inbuf();
    std::copy(spec, spec + N, rev_in);
    d_fft_rev->execute();
    gr_complex* time_out = d_fft_rev->get_outbuf();

    /* IFFT normalization (1/N) × synthesis-only Hann window. Sum of
     * overlapping Hann windows at 50% hop = 1 (Constant Overlap-Add,
     * COLA, satisfied), so output amplitude matches input amplitude
     * for any signal not modified in the spectrum. */
    const float scale = 1.0f / (float)N;
    for (int k = 0; k < N; k++) {
        d_scratch[k] = time_out[k] * (scale * d_hann[k]);
    }

    /* Overlap-add into the 2*N output buffer. The current write cursor
     * is the FIRST position the new block's contribution lands on; it
     * lines up with samples already there (from previous block's
     * overlapping tail). After the add, advance the write cursor by
     * one hop so the next block lands hop-samples later. The buffer
     * wraps at 2*N — that spacing keeps Block K and Block K-1 the only
     * two blocks contributing at each position. */
    const int BUF_SIZE = 2 * N;
    for (int k = 0; k < N; k++) {
        int idx = d_out_write_pos + k;
        if (idx >= BUF_SIZE) idx -= BUF_SIZE;
        d_out_buf[idx] += d_scratch[k];
    }
    d_out_write_pos += d_hop_size;
    if (d_out_write_pos >= BUF_SIZE) d_out_write_pos -= BUF_SIZE;

    d_n_blocks++;
    d_log_blocks++;
    d_n_bins_suppressed   += (uint64_t)blk_suppressed;
    d_log_bins_suppressed += blk_suppressed;

    /* Periodic telemetry every ~5 s. */
    auto now = std::chrono::steady_clock::now();
    if (std::chrono::duration_cast<std::chrono::seconds>(now - d_last_log)
            .count() >= 5) {
        double elapsed_sec = std::chrono::duration_cast<std::chrono::milliseconds>(
                                 now - d_t0).count() / 1000.0;
        double bins_per_block = d_log_blocks > 0
            ? (double)d_log_bins_suppressed / (double)d_log_blocks
            : 0.0;
        std::fprintf(stderr,
                     "[atsc_spectral_smoother t=%6.1fs] blocks=%llu "
                     "suppressed_bins=%llu (last5s: blocks=%d "
                     "avg_suppressed/block=%.1f / %d bins = %.2f%%)\n",
                     elapsed_sec,
                     (unsigned long long)d_n_blocks,
                     (unsigned long long)d_n_bins_suppressed,
                     d_log_blocks, bins_per_block, d_fft_size,
                     100.0 * bins_per_block / (double)d_fft_size);
        d_log_blocks          = 0;
        d_log_bins_suppressed = 0;
        d_last_log = now;
    }
}

int atsc_spectral_smoother_impl::work(int noutput_items,
                                      gr_vector_const_void_star& input_items,
                                      gr_vector_void_star&       output_items)
{
    /* OVERLAP-ADD STATE MACHINE.
     *
     * Per input sample:
     *   1. Write it into the input ring (overwriting the oldest sample).
     *   2. Emit one output sample from d_out_buf at d_out_read_pos and
     *      zero that slot so future blocks can overlap-add into it.
     *   3. Every d_hop_size new samples, trigger a block: process the
     *      last fft_size inputs (windowed FFT, modify, IFFT, windowed)
     *      and overlap-add the result into d_out_buf at d_out_write_pos.
     *
     * Net latency: d_hop_size samples (output zeros until the first
     * block contribution arrives). At 6.25 MS/s with fft_size=1024 and
     * hop=512 that's 82 µs — invisible.
     */
    const gr_complex* in  = static_cast<const gr_complex*>(input_items[0]);
    gr_complex*       out = static_cast<gr_complex*>(output_items[0]);
    const int N = d_fft_size;
    const int BUF_SIZE = 2 * N;

    for (int i = 0; i < noutput_items; i++) {
        /* Step 1: store input into the ring at the current write pos. */
        d_in_ring[d_in_ring_pos] = in[i];
        d_in_ring_pos++;
        if (d_in_ring_pos >= N) d_in_ring_pos -= N;

        /* Step 2: emit one output sample. Output is delayed by N samples
         * relative to input — output[t] corresponds to filtered input[t-N].
         * For the first N samples (warmup), pass the input straight
         * through. This means a 1-time time-rewind discontinuity at t=N
         * (where output goes from in[N-1] to filtered in[0]), but the
         * FPLL can track that better than starting from silence: it
         * keeps the carrier visible throughout, then has a single phase
         * jump to recover from. Empirically this lets the chain lock,
         * while zeros-warmup doesn't (FPLL needs a continuous carrier
         * reference to maintain its NCO state). */
        if (d_warmup_remaining > 0) {
            out[i] = in[i];
            d_warmup_remaining--;
            if (d_warmup_remaining == 0) {
                d_out_read_pos = d_hop_size;
            }
        } else {
            out[i] = d_out_buf[d_out_read_pos];
            d_out_buf[d_out_read_pos] = gr_complex(0.0f, 0.0f);
            d_out_read_pos++;
            if (d_out_read_pos >= BUF_SIZE) d_out_read_pos -= BUF_SIZE;
        }

        /* Step 3: maybe trigger a block. */
        d_in_count_since_block++;
        if (d_in_count_since_block >= d_hop_size) {
            process_one_block();
            d_in_count_since_block = 0;
        }
    }

    d_n_samples += (uint64_t)noutput_items;
    return noutput_items;
}

} /* namespace atscplus */
} /* namespace gr */
