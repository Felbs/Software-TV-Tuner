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
#include <fstream>
#include <cmath>

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
    d_taps[NPRETAPS] = 1.0f; // delta-function init — equalizer starts as pass-through

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
    // Tier-3 final: anti-windup + leakage. See decoder_tier3_log.md.
    static const double BETA = 0.00005;
    static const float  LEAK = 0.0005f;
    static const float  DIVERGENCE_BAIL = 50.0f;

    double sse = 0.0;
    for (int j = 0; j < nsamples; j++) {
        output_samples[j] = 0;
        volk_32f_x2_dot_prod_32f(
            &output_samples[j], &input_samples[j], &d_taps[0], NTAPS);

        float e = output_samples[j] - training_pattern[j];
        sse += (double)e * (double)e;

        float tmp_taps[NTAPS];
        volk_32f_s32f_multiply_32f(tmp_taps, &input_samples[j], BETA * e, NTAPS);
        volk_32f_x2_subtract_32f(&d_taps[0], &d_taps[0], tmp_taps, NTAPS);
    }

    // Tier-3 leakage: bound monotonic FS-only over-fit drift.
    float keep = 1.0f - LEAK;
    for (int k = 0; k < NTAPS; k++) d_taps[k] *= keep;

    // Tier-3 anti-windup: detect divergence and reset to delta.
    double tap_e = 0.0;
    for (int k = 0; k < NTAPS; k++) tap_e += (double)d_taps[k] * (double)d_taps[k];
    if (!std::isfinite(tap_e) || tap_e > (double)DIVERGENCE_BAIL*DIVERGENCE_BAIL) {
        for (int k = 0; k < NTAPS; k++) d_taps[k] = 0.0f;
        d_taps[NPRETAPS] = 1.0f;
        sse = 0.0;
        for (int j = 0; j < nsamples; j++) {
            output_samples[j] = (NPRETAPS+j < NTAPS+nsamples)
                ? input_samples[j+NPRETAPS] : 0.0f;
            float e = output_samples[j] - training_pattern[j];
            sse += (double)e * (double)e;
        }
    }

    d_field_sync_count++;
    d_last_fs_mse = (nsamples > 0) ? (float)(sse / nsamples) : 1e9f;
}

void atsc_equalizer_long_impl::adaptN_dd(const float* input_samples,
                                          float* output_samples,
                                          int nsamples)
{
    // Gated decision-directed LMS. Only updates the FF taps when the slicer
    // decision is plausibly correct (|y - decision| small). This keeps DD
    // adaptation from corrupting taps when the channel briefly drops below
    // slicing-reliable SNR. Sign convention matches adaptN(): error is
    // (y - target), so taps are SUBTRACTED with mu*err*x.
    //
    // Run-4 (2026-05-02) observation: BETA_DD=5e-6 with |e|<1.0 gate caused
    // taps to lock into a periodic wrong-decision solution that produced
    // degenerate TS output (292 distinct PIDs at 0% PAT). Reducing BETA and
    // tightening the gate fixes that. v3 raises BETA back up but keeps the
    // strict |e|<0.4 gate so wrong decisions are filtered.
    static const double BETA_DD = 0.000003;

    for (int j = 0; j < nsamples; j++) {
        float y;
        volk_32f_x2_dot_prod_32f(&y, &input_samples[j], &d_taps[0], NTAPS);
        output_samples[j] = y;

        // 8-VSB slicer: nearest of {-7,-5,-3,-1,1,3,5,7}
        float dec;
        if      (y >=  6.0f) dec =  7.0f;
        else if (y >=  4.0f) dec =  5.0f;
        else if (y >=  2.0f) dec =  3.0f;
        else if (y >=  0.0f) dec =  1.0f;
        else if (y >= -2.0f) dec = -1.0f;
        else if (y >= -4.0f) dec = -3.0f;
        else if (y >= -6.0f) dec = -5.0f;
        else                 dec = -7.0f;

        // Error in adaptN sign convention: e = y - target.
        float e = y - dec;

        // Reliability gate. If the slicer is far from the rail it lined up
        // with, the decision is probably wrong — skip the update. Half of
        // the symbol spacing (1.0) is a safe threshold.
        if (std::fabs(e) > DD_GATE_ABS_ERR) {
            continue;
        }

        // Tap update: d_taps -= BETA_DD * e * x
        float tmp_taps[NTAPS];
        volk_32f_s32f_multiply_32f(tmp_taps, &input_samples[j], BETA_DD * e, NTAPS);
        volk_32f_x2_subtract_32f(&d_taps[0], &d_taps[0], tmp_taps, NTAPS);
    }
}




void atsc_equalizer_long_impl::adaptN_dfe(const float* input_samples,
                                           float* output_samples,
                                           int nsamples)
{
    static const float LEVELS[8] = {-7.f, -5.f, -3.f, -1.f, 1.f, 3.f, 5.f, 7.f};
    static const double MU_FF  = 0.005;
    static const double MU_DFE = 0.005;

    // Initialize DFE state on first call
    if (!d_dfe_initialized) {
        for (int i = 0; i < NDFE; i++) { d_dfe_taps[i] = 0.0f; d_dec_hist[i] = 0.0f; }
        d_dfe_initialized = true;
    }

    for (int j = 0; j < nsamples; j++) {
        // FF FIR — convolve d_taps with NTAPS samples ending at input_samples[j]
        float ff_out;
        volk_32f_x2_dot_prod_32f(&ff_out, &input_samples[j], &d_taps[0], NTAPS);

        // DFE FIR — convolve dfe_taps with d_dec_hist (past decisions)
        float dfe_out = 0.0f;
        for (int i = 0; i < NDFE; i++) dfe_out += d_dfe_taps[i] * d_dec_hist[i];

        float y = ff_out - dfe_out;
        output_samples[j] = y;

        // Slice to nearest 8-VSB level
        float decision = LEVELS[0]; float bestd = std::abs(y - LEVELS[0]);
        for (int k = 1; k < 8; k++) {
            float d = std::abs(y - LEVELS[k]);
            if (d < bestd) { bestd = d; decision = LEVELS[k]; }
        }
        float err = decision - y;

        // Normalized LMS
        float norm_ff = 1e-6f;
        for (int i = 0; i < NTAPS; i++) norm_ff += input_samples[j+i] * input_samples[j+i];
        float norm_dfe = 1e-6f;
        for (int i = 0; i < NDFE; i++) norm_dfe += d_dec_hist[i] * d_dec_hist[i];

        float step_ff  = (float)(MU_FF  / norm_ff)  * err;
        float step_dfe = (float)(MU_DFE / norm_dfe) * err;

        for (int i = 0; i < NTAPS; i++) d_taps[i]     += step_ff  * input_samples[j+i];
        for (int i = 0; i < NDFE; i++)  d_dfe_taps[i] -= step_dfe * d_dec_hist[i];

        // Shift decision history
        for (int i = NDFE - 1; i > 0; i--) d_dec_hist[i] = d_dec_hist[i-1];
        d_dec_hist[0] = decision;
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

    std::vector<tag_t> tags;

    plinfo pli_in;
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
            } else if (!(d_flags & 0x0010)) {
                adaptN(data_mem, training_sequence1, data_mem2, KNOWN_FIELD_SYNC_LENGTH);
            }
        } else {
            // Tier-2 (2026-05-02): re-engage decision-directed LMS on data
            // segments, but ONLY after the field-sync trainer has converged
            // sufficiently. The previous unconditional DD path corrupted
            // taps because the slicer was guessing during cold start. Here
            // we require:
            //   * d_field_sync_count >= DD_MIN_FS_TRAININGS    (warm start)
            //   * d_last_fs_mse      <  DD_MAX_FS_MSE          (locked)
            // adaptN_dd() itself also gates per-sample on |error| to skip
            // updates whenever a decision looks unreliable. This should fix
            // (a) post-30s drift between field syncs, and (b) lock the EQ
            // into the basin of attraction once any field sync has trained.
            // Tier-3 patch 3: DD adaptation removed. DD kept taps stuck at
            // near-delta state. Reverting to plain filterN doubled deep-lock
            // convergence rate. See decoder_tier3_log.md.
            (void)DD_MIN_FS_TRAININGS;
            (void)DD_MAX_FS_MSE;
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
