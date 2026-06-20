/* -*- c++ -*- */
/*
 * Copyright 2014 Free Software Foundation, Inc.
 *
 * This file is part of GNU Radio
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_EQUALIZER_LONG_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_EQUALIZER_LONG_IMPL_H

#include "atsc_syminfo_impl.h"
#include <gnuradio/dtv/atsc_consts.h>
#include <gnuradio/atscplus/atsc_equalizer_long.h>

namespace gr {
namespace atscplus {

class atsc_equalizer_long_impl : public atsc_equalizer_long
{
private:
#ifdef ATSC_EQ_ECO
    static constexpr int NTAPS = 128;
#elif defined(ATSC_EQ_LONG_TAPS)
    // Wider equalizer: 32μs span (was 16μs). Captures longer multipath echoes.
    static constexpr int NTAPS = 512;
#else
    static constexpr int NTAPS = 256;
#endif
    static constexpr int NPRETAPS = (int)(NTAPS * 0.2);

    // Decision-feedback equalizer: number of feedback taps over past hard
    // decisions. Cancels post-cursor multipath echoes using already-decided
    // (noise-free) symbols — the lever a TV's demod has and our linear LMS
    // lacks. O(NFB) per symbol, so it holds real-time (unlike RLS's O(NTAPS^2)).
    static constexpr int NFB = 128;

    static constexpr int KNOWN_FIELD_SYNC_LENGTH = 4 + 511 + 3 * 63;

    float training_sequence1[KNOWN_FIELD_SYNC_LENGTH];
    float training_sequence2[KNOWN_FIELD_SYNC_LENGTH];

    void filterN(const float* input_samples, float* output_samples, int nsamples);
    void adaptN(const float* input_samples,
                const float* training_pattern,
                float* output_samples,
                int nsamples);
    // 2026-05-30 Confidence-gated decision-directed tracking. Runs on DATA
    // segments (between field syncs) when STVT_EQ_DD_MU>0. Filters, slices to
    // the nearest 8-VSB level, and does a normalized-LMS (NLMS) tap update
    // gated by slicer confidence (skip when |decision-y|>gate). Closes the
    // "taps frozen 312/313 of the time → drift to noise" hole in the
    // field-sync-only design, WITHOUT CMA's wrong-modulus convergence: DD
    // minimizes the same symbol-error objective as the FS-LMS anchor.
    void filterN_dd(const float* input_samples, float* output_samples, int nsamples);
    // 2026-06-19 Decision-feedback equalizer (STVT_EQ_DFE=1). Adds a feedback
    // FIR over past hard decisions to the existing feedforward FIR, cancelling
    // post-cursor ISI without the noise enhancement a linear equalizer suffers.
    // Both filters adapt by confidence-gated NLMS on data segments (the FS-LMS
    // anchor in adaptN still pulls the feedforward taps to ground truth). The
    // decision history resets per data segment so cross-field-sync staleness
    // can't feed the loop. Targets the +16-23pt headroom that only RLS reached,
    // at real-time cost. Tunables: STVT_EQ_DFE_MU, STVT_EQ_DFE_GATE.
    void filterN_dfe(const float* input_samples, float* output_samples, int nsamples);
    // 2026-05-30 RLS (Recursive Least Squares) field-sync adaptation. Optional
    // (STVT_EQ_RLS=1). Converges the equalizer far faster + tracks better than
    // LMS each field sync — the classic fix for "LMS too slow → drift". Runs
    // only on the ~728-symbol field-sync segment (per-symbol RLS over all 256
    // taps is feasible there, NOT over full data segments). Pairs with the DD
    // path for between-field tracking. Double-precision inverse-correlation
    // matrix for numerical stability.
    void adaptN_rls(const float* input_samples,
                    const float* training_pattern,
                    float* output_samples,
                    int nsamples);

    std::vector<float> d_taps;
    std::vector<float> d_fb_taps;    // DFE feedback taps over past decisions
    // DFE decision buffer: NFB zeros of history followed by one full segment of
    // decisions. Symbol j's feedback window is the contiguous slice [j, j+NFB),
    // so no per-symbol shift is needed (the memmove was the throughput killer).
    std::vector<float> d_dec_seg;
    std::vector<double> d_rls_P;   // NTAPS*NTAPS inverse-correlation matrix (RLS)
    bool   d_rls_inited = false;
    // Last-known-good snapshot: saved when taps look healthy (low energy,
    // finite, not in a divergence-induced delta-reset state). On
    // divergence, restored from snapshot instead of cold-resetting to a
    // delta function — which is what previously caused 30+s reconvergence
    // windows during channel walks.
    std::vector<float> d_taps_lkg;
    bool   d_lkg_valid = false;
    int    d_lkg_save_counter = 0;          // packets since last save
    double d_last_tap_e = 1.0;
    int    d_lkg_restore_count = 0;
    int    d_lkg_save_count = 0;
    int    d_divergence_count = 0;

    float data_mem[gr::dtv::ATSC_DATA_SEGMENT_LENGTH + NTAPS];
    float data_mem2[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];
    unsigned short d_flags;
    short d_segno;

    bool d_buff_not_filled = true;

    // FIX #3 (2026-05-23): coherent field-sync averaging.
    // Accumulate N field syncs' input buffers, run LMS on the average.
    // Signal=fixed (PN511/PN63), noise=iid → √N SNR gain.
    // Tunable via STVT_EQ_FS_AVG_DEPTH (default 1 = off).
    static constexpr int FS_ACC_LEN = gr::dtv::ATSC_DATA_SEGMENT_LENGTH + NTAPS;
    float d_fs_acc[FS_ACC_LEN];
    int   d_fs_count = 0;

public:
    atsc_equalizer_long_impl();
    ~atsc_equalizer_long_impl() override;

    void setup_rpc() override;

    std::vector<float> taps() const override;
    std::vector<float> data() const override;

    int general_work(int noutput_items,
                     gr_vector_int& ninput_items,
                     gr_vector_const_void_star& input_items,
                     gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_EQUALIZER_LONG_IMPL_H */
