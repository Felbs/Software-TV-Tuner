/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Tight FPLL fork - tunable loop alpha/beta + AFC time constant. */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_fpll_tight_impl.h"
#include <gnuradio/io_signature.h>
#include <gnuradio/math.h>
#include <gnuradio/sincos.h>
#include <cstdio>
#include <cmath>

namespace gr {
namespace atscplus {

atsc_fpll_tight::sptr atsc_fpll_tight::make(float rate, float alpha, float afc_tau_us)
{
    return gnuradio::make_block_sptr<atsc_fpll_tight_impl>(rate, alpha, afc_tau_us);
}

atsc_fpll_tight_impl::atsc_fpll_tight_impl(float rate, float alpha, float afc_tau_us)
    : sync_block("atscplus_atsc_fpll_tight",
                 io_signature::make(1, 1, sizeof(gr_complex)),
                 io_signature::make(1, 1, sizeof(float)))
{
    d_alpha = alpha;
    d_beta = alpha * alpha / 4.0f;
    d_afc.set_taps(1.0 - exp(-1.0 / rate / (afc_tau_us * 1e-6)));
    d_nco.set_freq((-3e6 + 0.309e6) / rate * 2 * GR_M_PI);
    d_nco.set_phase(0.0);
    std::fprintf(stderr,
                 "[fpll_tight] rate=%.0f alpha=%.5f beta=%.3e afc_tau_us=%.1f\n",
                 rate, d_alpha, d_beta, afc_tau_us);
}

atsc_fpll_tight_impl::~atsc_fpll_tight_impl() {}

int atsc_fpll_tight_impl::work(int noutput_items,
                               gr_vector_const_void_star& input_items,
                               gr_vector_void_star& output_items)
{
    auto in = static_cast<const gr_complex*>(input_items[0]);
    auto out = static_cast<float*>(output_items[0]);

    float a_cos, a_sin;
    float x;
    gr_complex result, filtered;

    for (int k = 0; k < noutput_items; k++) {
        d_nco.step();
        d_nco.sincos(&a_sin, &a_cos);

        gr::fast_cc_multiply(result, in[k], gr_complex(a_sin, a_cos));

        out[k] = result.real();

        filtered = d_afc.filter(result);
        x = gr::fast_atan2f(filtered.imag(), filtered.real());

        if (x > M_PI_2) x = M_PI_2;
        else if (x < -M_PI_2) x = -M_PI_2;

        d_nco.adjust_phase(d_alpha * x);
        d_nco.adjust_freq(d_beta * x);
    }

    return noutput_items;
}

} /* namespace atscplus */
} /* namespace gr */
