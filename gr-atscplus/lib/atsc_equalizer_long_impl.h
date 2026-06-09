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
// Equalizer tap-count override (2026-06-08). The data-segment path runs a full
// NTAPS VOLK dot-product per symbol (filterN_dd → filterN when DD is off) at
// ~10.76 M sym/s. Shrinking NTAPS cuts that cost ~proportionally; a strong,
// near-flat OTA channel (e.g. a +60 dB local mux) needs far fewer than 256 taps.
// MEASURED on a Pi 4 (radiopi, 2026-06-08): 256→96 gave NO useful speedup
// (0.31x vs 0.29x replay) — the equalizer is ~32% of load, NOT the wall; the
// FPLL/sync front-end threads are. Left as an inert opt-in (default 256, which
// also decodes cleaner) that may still help when stacked with front-end cuts on
// a faster ARM core. Override per-file (fast relink, not a full rebuild):
//   cmake -DATSC_EQ_NTAPS=96 ..    (try 64 / 96 / 128)
// NTAPS is used uniformly for all geometry/loop bounds, so changing only this
// constant is correct-by-construction. Default (no flag) is unchanged at 256.
#if defined(ATSC_EQ_NTAPS)
    static constexpr int NTAPS = ATSC_EQ_NTAPS;
#elif defined(ATSC_EQ_ECO)
    static constexpr int NTAPS = 128;
#elif defined(ATSC_EQ_LONG_TAPS)
    // Wider equalizer: 32μs span (was 16μs). Captures longer multipath echoes.
    static constexpr int NTAPS = 512;
#else
    static constexpr int NTAPS = 256;
#endif
    static_assert(NTAPS >= 16 && NTAPS <= 1024, "ATSC_EQ_NTAPS out of sane range");
    static constexpr int NPRETAPS = (int)(NTAPS * 0.2);

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
