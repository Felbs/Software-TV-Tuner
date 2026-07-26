/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_mer_probe_impl.h"
#include <gnuradio/io_signature.h>
#include <gnuradio/dtv/atsc_consts.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>

namespace gr {
namespace atscplus {

using gr::dtv::ATSC_DATA_SEGMENT_LENGTH;   // 832 symbols per segment (vector)

// RMS of the equiprobable 8-VSB levels {+-1,+-3,+-5,+-7}: sqrt((1+9+25+49)/4).
static constexpr double IDEAL_RMS = 4.58257569;

atsc_mer_probe::sptr atsc_mer_probe::make(const std::string& tag, uint64_t period)
{
    return gnuradio::make_block_sptr<atsc_mer_probe_impl>(tag, period);
}

atsc_mer_probe_impl::atsc_mer_probe_impl(const std::string& tag, uint64_t period)
    : sync_block("atscplus_atsc_mer_probe",
                 io_signature::make(
                     1, 1, ATSC_DATA_SEGMENT_LENGTH * sizeof(float)),
                 io_signature::make(0, 0, 0))   // pure sink
{
    d_tag    = tag;
    d_period = period ? period : 5000000ULL;
    if (const char* p = std::getenv("STVT_MER_PROBE_PERIOD")) {
        long v = std::atol(p);
        if (v > 0) d_period = (uint64_t)v;
    }

    d_init        = false;
    d_dc          = 0.0;
    d_g           = 1.0;
    d_acc         = 0.0;
    d_n           = 0;
    d_dd_err_rms  = 0.0;
    d_dd_mer      = 0.0;

    std::fprintf(stderr, "[mer-probe %s] armed (period=%llu symbols)\n",
                 d_tag.c_str(), (unsigned long long)d_period);
}

atsc_mer_probe_impl::~atsc_mer_probe_impl() = default;

int atsc_mer_probe_impl::work(int noutput_items,
                              gr_vector_const_void_star& input_items,
                              gr_vector_void_star& /*output_items*/)
{
    const float* in = static_cast<const float*>(input_items[0]);
    const size_t nsym = (size_t)noutput_items * (size_t)ATSC_DATA_SEGMENT_LENGTH;
    if (nsym == 0) return noutput_items;

    // Pass 1: buffer statistics for the slow DC / gain calibration.
    double sum = 0.0, sumsq = 0.0;
    for (size_t i = 0; i < nsym; i++) {
        const double v = in[i];
        sum += v;
        sumsq += v * v;
    }
    const double mean = sum / (double)nsym;
    const double rms  = std::sqrt(sumsq / (double)nsym);
    if (rms < 1e-9) return noutput_items;   // no signal — skip

    if (!d_init) {
        d_dc   = mean;
        d_g    = IDEAL_RMS / std::max(rms, 1e-9);
        d_init = true;
    } else {
        // Slow so fast multipath error is NOT absorbed, only gain/pilot drift.
        d_dc += 0.001 * (mean - d_dc);
        double centered_ms = sumsq / (double)nsym - 2.0 * d_dc * mean + d_dc * d_dc;
        const double rms_c = std::sqrt(std::max(centered_ms, 1e-18));
        const double g_inst = IDEAL_RMS / std::max(rms_c, 1e-9);
        d_g += 0.002 * (g_inst - d_g);
    }

    // Pass 2: decision-directed error against the nearest 8-VSB level.
    for (size_t i = 0; i < nsym; i++) {
        const double s = ((double)in[i] - d_dc) * d_g;
        double q = 2.0 * std::round((s - 1.0) * 0.5) + 1.0;   // nearest odd
        q = std::clamp(q, -7.0, 7.0);
        const double e = s - q;
        d_acc += e * e;
        d_n++;
    }

    if (d_n >= d_period) {
        const double err = std::sqrt(d_acc / (double)d_n);
        const double mer = 20.0 * std::log10(5.0 / std::max(err, 1e-9));
        d_dd_err_rms = err;
        d_dd_mer     = mer;
        std::fprintf(stderr, "[mer-probe %s] dd_err_rms=%.4f dd_mer=%.2f\n",
                     d_tag.c_str(), err, mer);
        d_acc = 0.0;
        d_n   = 0;
    }

    return noutput_items;
}

} /* namespace atscplus */
} /* namespace gr */
