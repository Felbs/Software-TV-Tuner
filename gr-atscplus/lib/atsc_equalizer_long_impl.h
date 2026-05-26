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

    static constexpr int KNOWN_FIELD_SYNC_LENGTH = 4 + 511 + 3 * 63;

    float training_sequence1[KNOWN_FIELD_SYNC_LENGTH];
    float training_sequence2[KNOWN_FIELD_SYNC_LENGTH];

    void filterN(const float* input_samples, float* output_samples, int nsamples);
    void adaptN(const float* input_samples,
                const float* training_pattern,
                float* output_samples,
                int nsamples);

    std::vector<float> d_taps;
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
