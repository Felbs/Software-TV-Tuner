/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_IMPL_H

#include "atsc_syminfo_impl.h"
#include <gnuradio/dtv/atsc_consts.h>
#include <gnuradio/atscplus/atsc_equalizer_pilot.h>

namespace gr {
namespace atscplus {

class atsc_equalizer_pilot_impl : public atsc_equalizer_pilot
{
private:
#ifdef ATSC_EQ_ECO
    static constexpr int NTAPS = 128;
#else
    static constexpr int NTAPS = 256;
#endif
    static constexpr int NPRETAPS = (int)(NTAPS * 0.2);

    static constexpr int KNOWN_FIELD_SYNC_LENGTH = 4 + 511 + 3 * 63;

    float training_sequence1[KNOWN_FIELD_SYNC_LENGTH];
    float training_sequence2[KNOWN_FIELD_SYNC_LENGTH];

    void filterN(const float* input_samples, float* output_samples, int nsamples);
    void estimate_taps_LS(const float* input_samples,
                          const float* training_pattern);

    std::vector<float> d_taps;

    float data_mem[gr::dtv::ATSC_DATA_SEGMENT_LENGTH + NTAPS];
    float data_mem2[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];
    unsigned short d_flags;
    short d_segno;

    bool d_buff_not_filled = true;
    float d_last_residual_rms = 0.0f;

public:
    atsc_equalizer_pilot_impl();
    ~atsc_equalizer_pilot_impl() override;

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

#endif /* INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_IMPL_H */
