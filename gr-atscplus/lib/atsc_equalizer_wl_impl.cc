/* -*- c++ -*- */
/*
 * gr-atscplus — widely-linear ATSC equalizer impl (2026-07-26)
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Minimal first pass: complex-in / real-out widely-linear FFE, LMS-trained on the
 * ATSC field sync. Two complex tap sets (main x + conjugate x*). Same segment /
 * plinfo interface as atsc_equalizer_long so it is a drop-in ONCE the chain feeds
 * it a carrier-corrected, symbol-timed COMPLEX stream (the next architectural step
 * — see lab/equalizer/README.md). Deliberately omits the long equalizer's FFT/DFE/
 * RLS/LKG/sheriff/mod12 machinery; those can be ported once WL is validated live.
 */
#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_equalizer_wl_impl.h"
#include "atsc_pnXXX_impl.h"
#include "atsc_types.h"
#include <gnuradio/io_signature.h>
#include <volk/volk.h>
#include <cmath>
#include <complex>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace gr {
namespace atscplus {
using gr::dtv::plinfo;
using gr::dtv::ATSC_DATA_SEGMENT_LENGTH;

atsc_equalizer_wl::sptr atsc_equalizer_wl::make()
{
    return gnuradio::make_block_sptr<atsc_equalizer_wl_impl>();
}

static float bin_map(int bit) { return bit ? +5 : -5; }

static void init_field_sync_common(float* p, int mask)
{
    int i = 0;
    p[i++] = bin_map(1); // data segment sync pulse
    p[i++] = bin_map(0);
    p[i++] = bin_map(0);
    p[i++] = bin_map(1);
    for (int j = 0; j < 511; j++) // PN511
        p[i++] = bin_map(atsc_pn511[j]);
    for (int j = 0; j < 63; j++) // PN63
        p[i++] = bin_map(atsc_pn63[j]);
    for (int j = 0; j < 63; j++) // PN63, toggled on field 2
        p[i++] = bin_map(atsc_pn63[j] ^ mask);
    for (int j = 0; j < 63; j++) // PN63
        p[i++] = bin_map(atsc_pn63[j]);
}

atsc_equalizer_wl_impl::atsc_equalizer_wl_impl()
    : gr::block("atscplus_atsc_equalizer_wl",
                io_signature::make2(2,
                                    2,
                                    ATSC_DATA_SEGMENT_LENGTH * sizeof(gr_complex),
                                    sizeof(plinfo)),
                io_signature::make2(2,
                                    2,
                                    ATSC_DATA_SEGMENT_LENGTH * sizeof(float),
                                    sizeof(plinfo)))
{
    init_field_sync_common(training_sequence1, 0);
    init_field_sync_common(training_sequence2, 1);

    d_w1.assign(NTAPS, gr_complex(0.0f, 0.0f));
    d_w2.assign(NTAPS, gr_complex(0.0f, 0.0f));
    d_w1[NPRETAPS] = gr_complex(1.0f, 0.0f); // delta init — start as pass-through
    std::memset(data_mem, 0, sizeof(data_mem));
    std::memset(data_mem2, 0, sizeof(data_mem2));
}

atsc_equalizer_wl_impl::~atsc_equalizer_wl_impl() {}

// y[k] = Re( sum_j w1[j] x[k+j] + w2[j] conj(x[k+j]) ), reference tap at NPRETAPS.
// SIMD: two volk complex dot products per output symbol (main + conjugate branch).
void atsc_equalizer_wl_impl::filterN(const gr_complex* in, float* out, int nsamples)
{
    for (int k = 0; k < nsamples; k++) {
        gr_complex a1, a2;
        volk_32fc_x2_dot_prod_32fc(&a1, in + k, d_w1.data(), NTAPS);           // w1·x
        volk_32fc_x2_conjugate_dot_prod_32fc(&a2, d_w2.data(), in + k, NTAPS); // w2·conj(x)
        out[k] = a1.real() + a2.real();
    }
}

// Widely-linear NLMS on the known field-sync symbols. For a REAL target d the
// augmented update is w1 += mu e conj(x), w2 += mu e x  (e real).
void atsc_equalizer_wl_impl::adaptN(const gr_complex* in,
                                    const float* training,
                                    float* out,
                                    int nsamples)
{
    const float mu = 0.5f;
    for (int k = 0; k < nsamples; k++) {
        const gr_complex* x = in + k;
        gr_complex a1, a2, ec;
        volk_32fc_x2_dot_prod_32fc(&a1, x, d_w1.data(), NTAPS);            // w1·x
        volk_32fc_x2_conjugate_dot_prod_32fc(&a2, d_w2.data(), x, NTAPS);  // w2·conj(x)
        volk_32fc_x2_conjugate_dot_prod_32fc(&ec, x, x, NTAPS);            // sum|x|^2
        float y = a1.real() + a2.real();
        out[k] = y;
        float energy = 2.0f * ec.real() + 1e-6f;         // augmented regressor energy
        float e = training[k] - y;
        float step = mu * e / energy;
        // LMS update runs only on field-sync segments (~1/313) — scalar is fine
        for (int j = 0; j < NTAPS; j++) {
            d_w1[j] += step * std::conj(x[j]);   // += mu*e*conj(x)/E
            d_w2[j] += step * x[j];              // += mu*e*x/E
        }
    }
    double e1 = 0.0, e2 = 0.0;
    for (int j = 0; j < NTAPS; j++) {
        e1 += std::norm(d_w1[j]);
        e2 += std::norm(d_w2[j]);
    }
    d_conj_frac = static_cast<float>(e2 / (e1 + e2 + 1e-12));
    static const bool TELEM = []() {
        const char* p = std::getenv("STVT_EQ_TELEM"); return p && std::atoi(p) != 0;
    }();
    if (TELEM) {
        static int fld = 0;
        if ((++fld % 40) == 0)
            std::fprintf(stderr, "[eq-wl] conj_frac=%.4f |w1|2=%.3f |w2|2=%.3f\n",
                         d_conj_frac, e1, e2);
    }
}

std::vector<float> atsc_equalizer_wl_impl::taps() const
{
    std::vector<float> t(2 * NTAPS);
    for (int j = 0; j < NTAPS; j++) {
        t[j] = std::abs(d_w1[j]);
        t[NTAPS + j] = std::abs(d_w2[j]);
    }
    return t;
}

std::vector<float> atsc_equalizer_wl_impl::data() const
{
    return std::vector<float>(data_mem2, data_mem2 + ATSC_DATA_SEGMENT_LENGTH);
}

float atsc_equalizer_wl_impl::conj_energy_fraction() const { return d_conj_frac; }

int atsc_equalizer_wl_impl::general_work(int noutput_items,
                                         gr_vector_int& ninput_items,
                                         gr_vector_const_void_star& input_items,
                                         gr_vector_void_star& output_items)
{
    auto in = static_cast<const gr_complex*>(input_items[0]);
    auto out = static_cast<float*>(output_items[0]);
    auto in_pl = static_cast<const plinfo*>(input_items[1]);
    auto out_pl = static_cast<plinfo*>(output_items[1]);

    int output_produced = 0;
    int i = 0;

    if (d_buff_not_filled) {
        std::memset(&data_mem[0], 0, NPRETAPS * sizeof(gr_complex));
        std::memcpy(&data_mem[NPRETAPS],
                    in + i * ATSC_DATA_SEGMENT_LENGTH,
                    ATSC_DATA_SEGMENT_LENGTH * sizeof(gr_complex));
        d_flags = in_pl[i].flags();
        d_segno = in_pl[i].segno();
        d_buff_not_filled = false;
        i++;
    }

    for (; i < noutput_items; i++) {
        // post-cursor taps come from the NEXT segment's leading samples
        std::memcpy(&data_mem[ATSC_DATA_SEGMENT_LENGTH + NPRETAPS],
                    in + i * ATSC_DATA_SEGMENT_LENGTH,
                    (NTAPS - NPRETAPS) * sizeof(gr_complex));

        if (d_segno == -1) {
            const float* trn =
                (d_flags & 0x0010) ? training_sequence2 : training_sequence1;
            adaptN(data_mem, trn, data_mem2, KNOWN_FIELD_SYNC_LENGTH);
            // field-sync segment trains only — produces no output
        } else {
            filterN(data_mem, data_mem2, ATSC_DATA_SEGMENT_LENGTH);
            std::memcpy(&out[output_produced * ATSC_DATA_SEGMENT_LENGTH],
                        data_mem2,
                        ATSC_DATA_SEGMENT_LENGTH * sizeof(float));
            out_pl[output_produced++] = plinfo(d_flags, d_segno);
        }

        // slide the window: keep NPRETAPS tail as new pre-cursor, load segment i
        std::memcpy(data_mem,
                    &data_mem[ATSC_DATA_SEGMENT_LENGTH],
                    NPRETAPS * sizeof(gr_complex));
        std::memcpy(&data_mem[NPRETAPS],
                    in + i * ATSC_DATA_SEGMENT_LENGTH,
                    ATSC_DATA_SEGMENT_LENGTH * sizeof(gr_complex));
        d_flags = in_pl[i].flags();
        d_segno = in_pl[i].segno();
    }

    consume_each(noutput_items);
    return output_produced;
}

} /* namespace atscplus */
} /* namespace gr */
