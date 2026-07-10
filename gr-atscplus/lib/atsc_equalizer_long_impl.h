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
#include <gnuradio/fft/fft.h>
#include <memory>
#include <vector>

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

    static constexpr int KNOWN_FIELD_SYNC_LENGTH = 4 + 511 + 3 * 63;

    float training_sequence1[KNOWN_FIELD_SYNC_LENGTH];
    float training_sequence2[KNOWN_FIELD_SYNC_LENGTH];

    // 2026-07-06 DFE v1 (docs/DFE_BLUEPRINT.md): feedback section state.
    // d_hist is double-length so the most recent NFB decisions are always
    // a contiguous slice (write at h and h+NFB). Data segments only in
    // v1; field syncs refill the history with KNOWN training symbols.
    static constexpr int NFB_MAX = 384;

    // ── FFT-convolution path (STVT_EQ_FFT=1, 2026-07-10) ──
    // Overlap-save replaces the 832 x NTAPS dot products per segment on
    // the passive (lean) path. Tap spectrum cached and invalidated by a
    // cheap fingerprint (energy + 3 sentinels) so no mutation site can
    // ever be missed. 2048 >= 832 + NTAPS - 1 for all NTAPS variants
    // up to 512 (832+511=1343).
    static constexpr int FFT_N = 2048;
    std::unique_ptr<gr::fft::fft_real_fwd> d_ffwd;
    std::unique_ptr<gr::fft::fft_real_rev> d_frev;
    std::vector<gr_complex> d_tap_spec;
    float d_tap_fp[4] = {0, 0, 0, 0};   // energy, first, mid, last
    bool d_tap_spec_valid = false;
    std::vector<float> d_fb;          // feedback taps (NFB long)
    std::vector<float> d_hist;        // decided symbols, 2*NFB ring
    int d_hpos = 0;
    bool d_dfe_suspend = false;       // E5 sheriff can pull the DFE offline
    void dfe_push(float sym, int nfb);

    // 2026-07-07 MOD-12 GUARD: each field emits exactly 312 data
    // segments (26x12), so at every FS the emitted count mod 12 must
    // be 0. A mid-stream discontinuity breaks that — and rotates the
    // downstream viterbi's rigid 12-batch mapping (the convicted
    // corruption/DEAF mechanism). Cure: drop <=11 segments to realign.
    int d_mod12_count = 0;            // data segments emitted, mod 12
    int d_mod12_drop = 0;             // segments still to drop
    int d_mod12_pending = -1;         // v2 debounce: phase must repeat

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
    // monotonic supervised-trainings-THIS-SESSION counter (never reset):
    // the DD warm-up hold keys on this, not d_lkg_valid, because the tap
    // cache pre-sets lkg_valid at load — before the AGC has settled or a
    // single live field sync has been seen (2026-07-10 explosion lesson)
    uint32_t d_fs_trained = 0;

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
