/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_DD_SOFT_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_DD_SOFT_IMPL_H

#include "atsc_syminfo_impl.h"
#include <gnuradio/dtv/atsc_consts.h>
#include <gnuradio/atscplus/atsc_equalizer_pilot_dd_soft.h>

namespace gr {
namespace atscplus {

class atsc_equalizer_pilot_dd_soft_impl : public atsc_equalizer_pilot_dd_soft
{
private:
    static constexpr int NTAPS = 256;
    static constexpr int NPRETAPS = (int)(NTAPS * 0.2);

    static constexpr int KNOWN_FIELD_SYNC_LENGTH = 4 + 511 + 3 * 63;

    float training_sequence1[KNOWN_FIELD_SYNC_LENGTH];
    float training_sequence2[KNOWN_FIELD_SYNC_LENGTH];

    void filterN(const float* input_samples, float* output_samples, int nsamples);
    void estimate_taps_LS(const float* input_samples,
                          const float* training_pattern);
    void filter_and_dd_update_soft(const float* input_samples,
                                   float* output_samples,
                                   int nsamples);

    std::vector<float> d_taps;
    std::vector<float> d_taps_lastfs;

    float data_mem[gr::dtv::ATSC_DATA_SEGMENT_LENGTH + NTAPS];
    float data_mem2[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];
    unsigned short d_flags;
    short d_segno;

    bool d_buff_not_filled = true;
    float d_last_residual_rms = 0.0f;

    // Tunables (env-loaded once in ctor).
    float d_mu;
    float d_gate;          // |e| at which conf reaches 0
    float d_inv_gate;      // 1.0 / d_gate, precomputed for hot path
    float d_leak;
    int   d_reset_fs;
    float d_ridge;
    int   d_debug;

    // Stats (printed at destruction).
    long  d_n_fs;
    long  d_n_data_segs;
    long  d_n_dd_active;       // samples with conf > 0
    long  d_n_dd_zero_conf;    // samples with conf == 0 (i.e. |e| >= gate)
    double d_sum_conf;         // for mean conf reporting
    long  d_n_dd_diverge;
    long  d_n_dd_resets;

public:
    atsc_equalizer_pilot_dd_soft_impl();
    ~atsc_equalizer_pilot_dd_soft_impl() override;

    std::vector<float> taps() const override;
    std::vector<float> data() const override;
    float last_residual_rms() const override;

    int general_work(int noutput_items,
                     gr_vector_int& ninput_items,
                     gr_vector_const_void_star& input_items,
                     gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_DD_SOFT_IMPL_H */
