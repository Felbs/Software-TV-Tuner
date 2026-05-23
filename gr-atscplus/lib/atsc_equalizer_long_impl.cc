/* -*- c++ -*- */
/*
 * Copyright 2014 Free Software Foundation, Inc.
 *
 * This file is part of GNU Radio
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_equalizer_long_impl.h"
#include "atsc_pnXXX_impl.h"
#include "atsc_types.h"
#include <gnuradio/io_signature.h>
#include <volk/volk.h>
#include <cmath>
#include <cstring>

namespace gr {
namespace atscplus {
using gr::dtv::plinfo;
using gr::dtv::ATSC_DATA_SEGMENT_LENGTH;

atsc_equalizer_long::sptr atsc_equalizer_long::make()
{
    return gnuradio::make_block_sptr<atsc_equalizer_long_impl>();
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

atsc_equalizer_long_impl::atsc_equalizer_long_impl()
    : gr::block("dtv_atsc_equalizer",
                io_signature::make2(
                    2, 2, ATSC_DATA_SEGMENT_LENGTH * sizeof(float), sizeof(plinfo)),
                io_signature::make2(
                    2, 2, ATSC_DATA_SEGMENT_LENGTH * sizeof(float), sizeof(plinfo)))
{
    init_field_sync_common(training_sequence1, 0);
    init_field_sync_common(training_sequence2, 1);

    d_taps.resize(NTAPS, 0.0f);
    d_taps[NPRETAPS] = 1.0f; // delta init — equalizer starts as pass-through

    d_taps_lkg.resize(NTAPS, 0.0f);
    d_lkg_valid = false;

    const int alignment_multiple = volk_get_alignment() / sizeof(float);
    set_alignment(std::max(1, alignment_multiple));
}

atsc_equalizer_long_impl::~atsc_equalizer_long_impl() {}

std::vector<float> atsc_equalizer_long_impl::taps() const { return d_taps; }

std::vector<float> atsc_equalizer_long_impl::data() const
{
    std::vector<float> ret(&data_mem2[0], &data_mem2[ATSC_DATA_SEGMENT_LENGTH - 1]);
    return ret;
}

void atsc_equalizer_long_impl::filterN(const float* input_samples,
                                  float* output_samples,
                                  int nsamples)
{
    for (int j = 0; j < nsamples; j++) {
        output_samples[j] = 0;
        volk_32f_x2_dot_prod_32f(
            &output_samples[j], &input_samples[j], &d_taps[0], NTAPS);
    }
}

void atsc_equalizer_long_impl::adaptN(const float* input_samples,
                                 const float* training_pattern,
                                 float* output_samples,
                                 int nsamples)
{
    // 2026-05-22 23:42: expose BETA/LEAK/DIVERGENCE_BAIL via env vars
    // so the chain can sweep optimal LMS step / leakage. Defaults match
    // prior hardcoded values. Read once and cached in static locals.
    static const double BETA = []() -> double {
        if (const char* p = std::getenv("STVT_EQ_BETA")) {
            char* e = nullptr; double v = std::strtod(p, &e);
            if (e != p) return v;
        }
        return 5e-5;
    }();
    static const float  LEAK = []() -> float {
        if (const char* p = std::getenv("STVT_EQ_LEAK")) {
            char* e = nullptr; double v = std::strtod(p, &e);
            if (e != p) return (float)v;
        }
        return 5e-4f;
    }();
    static const float  DIVERGENCE_BAIL = []() -> float {
        if (const char* p = std::getenv("STVT_EQ_DIVERGE")) {
            char* e = nullptr; double v = std::strtod(p, &e);
            if (e != p) return (float)v;
        }
        return 50.0f;
    }();
    // 2026-05-23 LKG (Last Known Good) tap restoration.
    // When set STVT_EQ_LKG=1, the equalizer keeps a snapshot of its last
    // confidently-good tap state (small LMS error during a clean field sync).
    // On divergence (tap_e too high / NaN), restore from snapshot instead
    // of resetting to delta. Idea: during RF fades the equalizer gets
    // pulled into bad states; reverting to a known good shape lets it
    // recover faster than re-converging from delta.
    static const bool LKG_ENABLED = []() -> bool {
        const char* p = std::getenv("STVT_EQ_LKG");
        return p && std::atoi(p) != 0;
    }();
    static const float LKG_GOOD_RMS_THRESHOLD = []() -> float {
        if (const char* p = std::getenv("STVT_EQ_LKG_RMS")) {
            char* e = nullptr; double v = std::strtod(p, &e);
            if (e != p) return (float)v;
        }
        return 1.5f;   // error RMS below this counts as "good"
    }();
    static bool _logged = []() {
        std::fprintf(stderr,
                     "[atsc_equalizer_long] tunable params: BETA=%g LEAK=%g DIVERGENCE_BAIL=%g LKG=%d LKG_RMS=%g\n",
                     BETA, LEAK, DIVERGENCE_BAIL,
                     (int)LKG_ENABLED, LKG_GOOD_RMS_THRESHOLD);
        return true;
    }();

    // Accumulate RMS error during this adapt batch for LKG quality assessment.
    double err_sq_sum = 0.0;

    for (int j = 0; j < nsamples; j++) {
        output_samples[j] = 0;
        volk_32f_x2_dot_prod_32f(
            &output_samples[j], &input_samples[j], &d_taps[0], NTAPS);

        float e = output_samples[j] - training_pattern[j];
        err_sq_sum += (double)e * (double)e;

        float tmp_taps[NTAPS];
        volk_32f_s32f_multiply_32f(tmp_taps, &input_samples[j], BETA * e, NTAPS);
        volk_32f_x2_subtract_32f(&d_taps[0], &d_taps[0], tmp_taps, NTAPS);
    }

    float keep = 1.0f - LEAK;
    for (int k = 0; k < NTAPS; k++) d_taps[k] *= keep;

    // LKG snapshot: if THIS adapt batch had low error, this tap state is
    // probably good. Save it for future divergence recovery.
    if (LKG_ENABLED && nsamples > 0) {
        double err_rms = std::sqrt(err_sq_sum / (double)nsamples);
        if (err_rms < (double)LKG_GOOD_RMS_THRESHOLD) {
            for (int k = 0; k < NTAPS; k++) d_taps_lkg[k] = d_taps[k];
            d_lkg_valid = true;
        }
    }

    double tap_e = 0.0;
    for (int k = 0; k < NTAPS; k++) tap_e += (double)d_taps[k] * (double)d_taps[k];
    if (!std::isfinite(tap_e) || tap_e > (double)DIVERGENCE_BAIL*DIVERGENCE_BAIL) {
        // Divergence: restore from LKG if available, otherwise reset to delta.
        if (LKG_ENABLED && d_lkg_valid) {
            static uint64_t lkg_restores = 0;
            lkg_restores++;
            for (int k = 0; k < NTAPS; k++) d_taps[k] = d_taps_lkg[k];
            if (lkg_restores <= 5 || (lkg_restores & 0x3F) == 0) {
                std::fprintf(stderr,
                             "[atsc_equalizer_long] LKG restore #%llu (tap_e was %g)\n",
                             (unsigned long long)lkg_restores, tap_e);
            }
        } else {
            for (int k = 0; k < NTAPS; k++) d_taps[k] = 0.0f;
            d_taps[NPRETAPS] = 1.0f;
        }
        for (int j = 0; j < nsamples; j++) {
            output_samples[j] = (NPRETAPS+j < NTAPS+nsamples)
                ? input_samples[j+NPRETAPS] : 0.0f;
        }
    }
}

int atsc_equalizer_long_impl::general_work(int noutput_items,
                                      gr_vector_int& ninput_items,
                                      gr_vector_const_void_star& input_items,
                                      gr_vector_void_star& output_items)
{
    auto in = static_cast<const float*>(input_items[0]);
    auto out = static_cast<float*>(output_items[0]);
    auto in_pl = static_cast<const plinfo*>(input_items[1]);
    auto out_pl = static_cast<plinfo*>(output_items[1]);

    int output_produced = 0;
    int i = 0;

    if (d_buff_not_filled) {
        memset(&data_mem[0], 0, NPRETAPS * sizeof(float));
        memcpy(&data_mem[NPRETAPS],
               in + i * ATSC_DATA_SEGMENT_LENGTH,
               ATSC_DATA_SEGMENT_LENGTH * sizeof(float));

        d_flags = in_pl[i].flags();
        d_segno = in_pl[i].segno();

        d_buff_not_filled = false;
        i++;
    }

    for (; i < noutput_items; i++) {

        memcpy(&data_mem[ATSC_DATA_SEGMENT_LENGTH + NPRETAPS],
               in + i * ATSC_DATA_SEGMENT_LENGTH,
               (NTAPS - NPRETAPS) * sizeof(float));

        if (d_segno == -1) {
            if (d_flags & 0x0010) {
                adaptN(data_mem, training_sequence2, data_mem2, KNOWN_FIELD_SYNC_LENGTH);
            } else {
                adaptN(data_mem, training_sequence1, data_mem2, KNOWN_FIELD_SYNC_LENGTH);
            }
        } else {
            filterN(data_mem, data_mem2, ATSC_DATA_SEGMENT_LENGTH);

            memcpy(&out[output_produced * ATSC_DATA_SEGMENT_LENGTH],
                   data_mem2,
                   ATSC_DATA_SEGMENT_LENGTH * sizeof(float));

            plinfo pli_out(d_flags, d_segno);
            out_pl[output_produced++] = pli_out;
        }

        memcpy(data_mem, &data_mem[ATSC_DATA_SEGMENT_LENGTH], NPRETAPS * sizeof(float));
        memcpy(&data_mem[NPRETAPS],
               in + i * ATSC_DATA_SEGMENT_LENGTH,
               ATSC_DATA_SEGMENT_LENGTH * sizeof(float));

        d_flags = in_pl[i].flags();
        d_segno = in_pl[i].segno();
    }

    consume_each(noutput_items);
    return output_produced;
}

void atsc_equalizer_long_impl::setup_rpc()
{
#ifdef GR_CTRLPORT
    add_rpc_variable(
        rpcbasic_sptr(new rpcbasic_register_get<atsc_equalizer_long, std::vector<float>>(
            alias(),
            "taps",
            &atsc_equalizer_long::taps,
            pmt::make_f32vector(1, -10),
            pmt::make_f32vector(1, 10),
            pmt::make_f32vector(1, 0),
            "",
            "Equalizer Taps",
            RPC_PRIVLVL_MIN,
            DISPTIME)));

    add_rpc_variable(
        rpcbasic_sptr(new rpcbasic_register_get<atsc_equalizer_long, std::vector<float>>(
            alias(),
            "data",
            &atsc_equalizer_long::data,
            pmt::make_f32vector(1, -10),
            pmt::make_f32vector(1, 10),
            pmt::make_f32vector(1, 0),
            "",
            "Post-equalizer Data",
            RPC_PRIVLVL_MIN,
            DISPTIME)));
#endif /* GR_CTRLPORT */
}

} /* namespace atscplus */
} /* namespace gr */
