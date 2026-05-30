/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */

#ifndef INCLUDED_ATSCPLUS_ATSC_SPECTRAL_SMOOTHER_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_SPECTRAL_SMOOTHER_IMPL_H

#include <gnuradio/atscplus/atsc_spectral_smoother.h>
#include <gnuradio/fft/fft.h>
#include <chrono>
#include <complex>
#include <cstdint>
#include <memory>
#include <vector>

namespace gr {
namespace atscplus {

class atsc_spectral_smoother_impl : public atsc_spectral_smoother
{
private:
    /* Tunables (constructor + env overrides). */
    double d_sample_rate;
    int    d_fft_size;
    int    d_neighborhood;
    double d_threshold;
    double d_pilot_offset_hz;
    double d_pilot_guard_hz;
    /* Pre-computed bin range to EXCLUDE from outlier suppression. The
     * ATSC pilot tone is itself a narrowband peak vastly stronger than
     * the local median; treating it as an outlier kills the FPLL's
     * carrier reference. Bins in [d_pilot_bin_lo, d_pilot_bin_hi]
     * (in the FFT's natural ordering — not FFT-shifted) are skipped. */
    int    d_pilot_bin_lo;
    int    d_pilot_bin_hi;

    /* FFT facilities. */
    std::unique_ptr<gr::fft::fft_complex_fwd> d_fft_fwd;
    std::unique_ptr<gr::fft::fft_complex_rev> d_fft_rev;

    /* OVERLAP-ADD STATE.
     * Hop size = fft_size / 2 (50% overlap). With Hann window applied
     * at both analysis and synthesis, the Constant Overlap-Add (COLA)
     * sum equals 1 so the smoother preserves the signal in regions
     * where it doesn't modify any bins. Block boundaries are smoothed
     * out by the overlapping windows — the discontinuities that broke
     * v1 of the smoother are eliminated.
     *
     * Layout:
     *   d_in_ring: circular buffer of last fft_size input samples
     *   d_in_ring_pos: write position in d_in_ring [0, fft_size)
     *   d_in_count_since_block: # new inputs since last FFT trigger
     *
     *   d_out_buf: circular buffer of fft_size pending output samples.
     *              Each FFT block adds (overlap-adds) its windowed
     *              output to fft_size positions starting at the WRITE
     *              cursor. The READ cursor emits one sample per
     *              consumed input. Samples slots are zeroed after read
     *              so they're ready for the next overlapping block's
     *              contribution.
     *   d_out_read_pos:  position to emit from [0, fft_size)
     *   d_out_write_pos: where next block adds its contribution
     *
     *   d_hann: precomputed analysis+synthesis window (Hann shape).
     *   d_scratch: working buffer for the windowed block.
     */
    std::vector<gr_complex> d_in_ring;
    int                     d_in_ring_pos;
    int                     d_in_count_since_block;
    int                     d_hop_size;

    /* d_out_buf is sized 2*fft_size (not fft_size). With size = fft_size,
     * the buffer wraps every N samples = 2 hops. Block K writes to all
     * positions (since N writes span N positions mod N = full buffer),
     * so Block K and Block K-2 contribute to the SAME position, even
     * though they correspond to input times N samples apart. Result:
     * 3 blocks per position (instead of 2), garbage OLA reconstruction.
     * Size 2*N spaces the cycle so only 2 consecutive blocks overlap. */
    std::vector<gr_complex> d_out_buf;
    int                     d_out_read_pos;
    int                     d_out_write_pos;
    int                     d_warmup_remaining;  /* pass through input for first N samples */

    std::vector<float>      d_hann;
    std::vector<gr_complex> d_scratch;

    /* Scratch for per-block magnitude computation. */
    std::vector<float>      d_mag_sq;     // |X[k]|² per bin
    std::vector<float>      d_neighbors;  // working buffer for median

    /* Cached median per bin — recomputed every d_median_recompute_blocks
     * blocks rather than every block. Outliers from cellular leakage,
     * AM stations, etc. shift on the seconds timescale, but the median
     * is recomputed at the block (microsecond) rate. Caching the median
     * gives a 4x speedup of the median path at no detectable quality
     * cost. STVT_SPECTRAL_MEDIAN_EVERY env override. */
    std::vector<float>      d_cached_median;       // last computed median per bin
    int                     d_median_recompute_blocks;  // recompute every Nth block
    int                     d_blocks_since_median;      // counter

    /* Telemetry. */
    uint64_t d_n_samples;
    uint64_t d_n_blocks;
    uint64_t d_n_bins_suppressed;
    int      d_log_blocks;
    int      d_log_bins_suppressed;
    std::chrono::steady_clock::time_point d_t0;
    std::chrono::steady_clock::time_point d_last_log;

    /* Process one accumulated d_in_buf into d_out_buf. */
    void process_one_block();

public:
    atsc_spectral_smoother_impl(double sample_rate,
                                int    fft_size,
                                int    neighborhood,
                                double threshold,
                                double pilot_offset_hz,
                                double pilot_guard_hz);
    ~atsc_spectral_smoother_impl() override;

    uint64_t num_samples()        const override { return d_n_samples; }
    uint64_t num_blocks()         const override { return d_n_blocks; }
    uint64_t num_bins_suppressed() const override { return d_n_bins_suppressed; }

    int work(int noutput_items,
             gr_vector_const_void_star& input_items,
             gr_vector_void_star&       output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif
