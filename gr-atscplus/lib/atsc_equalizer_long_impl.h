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
#include <chrono>

namespace gr {
namespace atscplus {

class atsc_equalizer_long_impl : public atsc_equalizer_long
{
private:
    static constexpr int NTAPS = 256;
    // Real bug was in general_work — adaptN_dd was re-adapting taps every
    // data segment with noisy decisions instead of just applying trained
    // taps. Upstream calls filterN(); we now match. Tap ratio matches
    // upstream too (51 pre-cursor, 205 post-cursor).
    static constexpr int NPRETAPS = (int)(NTAPS * 0.2);

    // DFE (decision-feedback) settings — ported from atsc_decoder.py
    static constexpr int NDFE = 64;
    float d_dfe_taps[NDFE];
    float d_dec_hist[NDFE];
    bool d_dfe_initialized = false;

    // the length of the field sync pattern that we know unequivocally
    static constexpr int KNOWN_FIELD_SYNC_LENGTH = 4 + 511 + 3 * 63;

    float training_sequence1[KNOWN_FIELD_SYNC_LENGTH];
    float training_sequence2[KNOWN_FIELD_SYNC_LENGTH];

    // Tier-2 DD-LMS state. We only enable decision-directed adaptation after
    // the field-sync trainer has run several times AND the residual error is
    // small enough to trust the slicer decisions. Otherwise wrong decisions
    // re-enforce themselves and corrupt the taps (the previous failure mode).
    int   d_field_sync_count   = 0;     // total field-sync adaptN() calls
    float d_last_fs_mse        = 1e9f;  // MSE from most recent adaptN()
    // Tier-2 v3: looser MSE gate (DD helps even at partial lock, run 6 shows
    // late convergence with this enabled) but strict per-symbol gate to block
    // wrong-decision feedback loops.
    static constexpr int   DD_MIN_FS_TRAININGS = 4;     // wait for 4 field syncs ~1.5s
    static constexpr float DD_MAX_FS_MSE       = 6.0f;  // run DD any time field-sync MSE is finite
    static constexpr float DD_GATE_ABS_ERR     = 0.4f;  // strict: only confident decisions
    static constexpr float DD_LEAK             = 0.0f;  // leak disabled — was driving taps to 0

    // Tier-16 (2026-05-02): hyperparameter sweep constants. Read from env at
    // block construction. Defaults match the Tier-3+Tier-10 shipped values.
    //   MAGIC_BETA      adaptN LMS step (default 5e-5)
    //   MAGIC_LEAK      adaptN per-FS leakage (default 5e-4)
    //   MAGIC_DIV_BAIL  anti-windup tap-norm threshold (default 10.0)
    double d_magic_beta;
    float  d_magic_leak;
    float  d_magic_div_bail;

    // Tier-20 (2026-05-02): per-FS-interval data-segment dev_rms instrumentation.
    // Gated on ATSCPLUS_TIER20_LOG=1; default OFF.
    // Tracks per-data-segment (post-equalizer) deviation RMS from the nearest
    // 8-VSB rail after subtracting the post-pilot 1.25 mean offset, accumulated
    // over the 312 data segments BETWEEN field syncs. On each FS-pass, dumps
    // CSV-friendly stderr line with fs_pass_idx, wall_time_sec since
    // block-construction, tap_norm, fs_mse, data_dev_rms (mean of 312),
    // data_dev_max (max of 312), and the count of data segments observed.
    bool d_tier20_log_enabled = false;
    std::chrono::steady_clock::time_point d_tier20_t0;
    int    d_tier20_fs_pass_idx       = 0;
    int    d_tier20_data_seg_count    = 0;     // segments seen since last FS
    double d_tier20_dev_rms_sum       = 0.0;   // sum of per-segment dev_rms
    float  d_tier20_dev_rms_max       = 0.0f;  // max of per-segment dev_rms

    // Helpers for Tier-20 — defined in .cc to keep header light.
    void tier20_accumulate_data_seg(const float* y, int nsamples);
    void tier20_emit_fs_line(float tap_norm);

    void filterN(const float* input_samples, float* output_samples, int nsamples);
    void adaptN(const float* input_samples,
                const float* training_pattern,
                float* output_samples,
                int nsamples);
    void adaptN_dd(const float* input_samples, float* output_samples, int nsamples);
    void adaptN_dfe(const float* input_samples, float* output_samples, int nsamples);

    std::vector<float> d_taps;

    float data_mem[gr::dtv::ATSC_DATA_SEGMENT_LENGTH + NTAPS]; // Buffer for previous data packet
    float data_mem2[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];
    unsigned short d_flags;
    short d_segno;

    bool d_buff_not_filled = true;

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
