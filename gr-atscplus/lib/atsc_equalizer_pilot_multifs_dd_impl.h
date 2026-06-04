/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_MULTIFS_DD_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_MULTIFS_DD_IMPL_H

#include "atsc_syminfo_impl.h"
#include <gnuradio/dtv/atsc_consts.h>
#include <gnuradio/atscplus/atsc_equalizer_pilot_multifs_dd.h>
#include <Eigen/Dense>

namespace gr {
namespace atscplus {

class atsc_equalizer_pilot_multifs_dd_impl : public atsc_equalizer_pilot_multifs_dd
{
private:
    static constexpr int NTAPS = 256;
    static constexpr int NPRETAPS = (int)(NTAPS * 0.2);
    static constexpr int KNOWN_FIELD_SYNC_LENGTH = 4 + 511 + 3 * 63;

    float training_sequence1[KNOWN_FIELD_SYNC_LENGTH];
    float training_sequence2[KNOWN_FIELD_SYNC_LENGTH];

    void filterN(const float* input_samples, float* output_samples, int nsamples);
    void accumulate_FS(const float* input_samples, const float* training_pattern);
    void solve_taps_LS();
    void filter_and_softdd(const float* input_samples, float* output_samples, int nsamples);

    std::vector<float> d_taps;
    std::vector<float> d_taps_lastsolve;

    float data_mem[gr::dtv::ATSC_DATA_SEGMENT_LENGTH + NTAPS];
    float data_mem2[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];
    unsigned short d_flags;
    short d_segno;

    bool d_buff_not_filled = true;
    float d_last_residual_rms = 0.0f;

    int   d_K;
    float d_ridge;
    float d_mu;
    float d_gate;
    float d_inv_gate;
    int   d_debug;

    Eigen::MatrixXd d_AtA;
    Eigen::VectorXd d_Atb;
    int   d_fs_in_window;

    long  d_n_fs;
    long  d_n_solves;
    long  d_n_data_segs;
    long  d_n_dd_active;
    long  d_n_dd_zero;
    double d_sum_conf;
    long  d_n_dd_diverge;

public:
    atsc_equalizer_pilot_multifs_dd_impl();
    ~atsc_equalizer_pilot_multifs_dd_impl() override;

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

#endif /* INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_MULTIFS_DD_IMPL_H */
