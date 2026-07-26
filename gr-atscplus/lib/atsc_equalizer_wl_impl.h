/* -*- c++ -*- */
/*
 * gr-atscplus — widely-linear ATSC equalizer impl (2026-07-26)
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
#ifndef INCLUDED_ATSCPLUS_ATSC_EQUALIZER_WL_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_EQUALIZER_WL_IMPL_H

#include "atsc_syminfo_impl.h"
#include <gnuradio/atscplus/atsc_equalizer_wl.h>
#include <gnuradio/dtv/atsc_consts.h>
#include <gnuradio/gr_complex.h>
#include <vector>

namespace gr {
namespace atscplus {

class atsc_equalizer_wl_impl : public atsc_equalizer_wl
{
private:
    // 128 taps (was 256): the widely-linear filter runs TWO complex dot products
    // per symbol (main + conjugate), so it is ~2x a normal equalizer. 128 keeps it
    // within the real-time budget at 10.76 Msym/s (12 us echo reach — plenty for
    // the fading-limited channels WL targets). Bump back to 256 if CPU allows.
    static constexpr int NTAPS = 128;
    static constexpr int NPRETAPS = (int)(NTAPS * 0.2);
    static constexpr int KNOWN_FIELD_SYNC_LENGTH = 4 + 511 + 3 * 63;

    float training_sequence1[KNOWN_FIELD_SYNC_LENGTH];
    float training_sequence2[KNOWN_FIELD_SYNC_LENGTH];

    // Widely-linear taps: y[k] = Re( sum_j w1[j]*x[k+j] + w2[j]*conj(x[k+j]) )
    std::vector<gr_complex> d_w1;   // main branch  (x)
    std::vector<gr_complex> d_w2;   // conjugate branch (x*)

    // sliding complex window: [NPRETAPS pre | segment | post]
    gr_complex data_mem[gr::dtv::ATSC_DATA_SEGMENT_LENGTH + NTAPS];
    float data_mem2[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];

    unsigned short d_flags = 0;
    short d_segno = 0;
    bool d_buff_not_filled = true;
    float d_conj_frac = 0.0f;

    // main branch = volk_32fc_x2_dot_prod (w1·x); conjugate branch =
    // volk_32fc_x2_conjugate_dot_prod (w2·conj(x)) — no separate conj buffer.
    void filterN(const gr_complex* in, float* out, int nsamples);
    void adaptN(const gr_complex* in, const float* training, float* out, int nsamples);

public:
    atsc_equalizer_wl_impl();
    ~atsc_equalizer_wl_impl() override;

    std::vector<float> taps() const override;
    std::vector<float> data() const override;
    float conj_energy_fraction() const override;

    int general_work(int noutput_items,
                     gr_vector_int& ninput_items,
                     gr_vector_const_void_star& input_items,
                     gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_EQUALIZER_WL_IMPL_H */
