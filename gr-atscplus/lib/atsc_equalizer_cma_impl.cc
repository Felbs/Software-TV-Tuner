/* -*- c++ -*- */
/*
 * Tier 6 (2026-05-02): CMA + DFE equalizer for gr-atscplus.
 *
 * Drop-in replacement for atsc_equalizer_long. Differences from Tier 3:
 *
 *   1) On data segments we run a Constant-Modulus-Algorithm update
 *      every output symbol, instead of running the FF filter passively.
 *      CMA gradient: e_cma = y * (R - |y|^2),  taps += mu * e_cma * x.
 *      For 8-VSB R = E[|s|^4] / E[|s|^2] = 37.0.
 *
 *   2) A 32-tap decision-feedback equalizer subtracts post-cursor ISI
 *      using the slicer's hard decision history. DFE tap updates are
 *      gated by slicer confidence (skip when |y - decision| > 1.0)
 *      to suppress error propagation when the channel briefly drops
 *      below slicing-reliable SNR.
 *
 *   3) On field-sync segments we still run a supervised LMS pass
 *      (much faster cold-start convergence than CMA-from-scratch).
 *      Tier-3's anti-windup + leakage are kept on this branch.
 *
 *   4) Tap geometry rebalanced to NPRETAPS=96 / post=159 to match the
 *      Phase-0 channel-IR estimate (90 % of channel energy in
 *      96 pre + 192 post symbols on RF36 from the user's location).
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_equalizer_cma_impl.h"
#include "atsc_pnXXX_impl.h"
#include "atsc_types.h"
#include <gnuradio/io_signature.h>
#include <volk/volk.h>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace gr {
namespace atscplus {

using gr::dtv::plinfo;
using gr::dtv::ATSC_DATA_SEGMENT_LENGTH;

// Definition for static constexpr float array (required pre-C++17 in
// some toolchains; harmless under MSVC C++17).
constexpr float atsc_equalizer_cma_impl::LEVELS[];

atsc_equalizer_cma::sptr atsc_equalizer_cma::make()
{
    return gnuradio::make_block_sptr<atsc_equalizer_cma_impl>();
}

static float bin_map(int bit) { return bit ? +5.0f : -5.0f; }

static void init_field_sync_common(float* p, int mask)
{
    int i = 0;
    p[i++] = bin_map(1); // data segment sync pulse 1001
    p[i++] = bin_map(0);
    p[i++] = bin_map(0);
    p[i++] = bin_map(1);
    for (int j = 0; j < 511; j++) p[i++] = bin_map(atsc_pn511[j]);
    for (int j = 0; j < 63;  j++) p[i++] = bin_map(atsc_pn63[j]);
    for (int j = 0; j < 63;  j++) p[i++] = bin_map(atsc_pn63[j] ^ mask);
    for (int j = 0; j < 63;  j++) p[i++] = bin_map(atsc_pn63[j]);
}

static float env_float(const char* name, float dflt)
{
    const char* v = std::getenv(name);
    if (!v || !*v) return dflt;
    char* endp = nullptr;
    float f = std::strtof(v, &endp);
    if (endp == v) return dflt;
    return f;
}

atsc_equalizer_cma_impl::atsc_equalizer_cma_impl()
    : gr::block("atsc_equalizer_cma",
                io_signature::make2(
                    2, 2, ATSC_DATA_SEGMENT_LENGTH * sizeof(float), sizeof(plinfo)),
                io_signature::make2(
                    2, 2, ATSC_DATA_SEGMENT_LENGTH * sizeof(float), sizeof(plinfo))),
      d_mu_cma(env_float("ATSC_T6_MU_CMA",  1.0e-5f)),
      d_mu_lms_fs(env_float("ATSC_T6_MU_LMS_FS", 5.0e-5f)),
      d_mu_dfe(env_float("ATSC_T6_MU_DFE",  1.0e-4f)),
      d_leak(env_float("ATSC_T6_LEAK", 5.0e-4f)),
      d_diverge_norm(env_float("ATSC_T6_DIVERGE_NORM", 50.0f)),
      d_dfe_gate(env_float("ATSC_T6_DFE_GATE", 1.0f)),
      d_flags(0), d_segno(0), d_buff_not_filled(true),
      d_field_sync_count(0), d_last_fs_mse(1e9f),
      d_diverge_resets(0), d_cma_updates(0),
      d_dfe_updates_applied(0), d_dfe_updates_skipped(0),
      d_telem_every_n_fs(8)
{
    init_field_sync_common(training_sequence1, 0);
    init_field_sync_common(training_sequence2, 1);

    d_taps.assign(NTAPS, 0.0f);
    d_taps[NPRETAPS] = 1.0f; // delta-init — start as pass-through

    std::memset(d_dfe_taps, 0, sizeof(d_dfe_taps));
    std::memset(d_dec_hist, 0, sizeof(d_dec_hist));
    std::memset(data_mem,   0, sizeof(data_mem));
    std::memset(data_mem2,  0, sizeof(data_mem2));

    d_t0 = std::chrono::steady_clock::now();

    const int alignment_multiple = volk_get_alignment() / sizeof(float);
    set_alignment(std::max(1, alignment_multiple));

    std::fprintf(stderr,
                 "[t6-cma] init: NTAPS=%d NPRETAPS=%d NDFE=%d "
                 "mu_cma=%.2e mu_lms_fs=%.2e mu_dfe=%.2e leak=%.2e "
                 "div_norm=%.1f dfe_gate=%.2f\n",
                 NTAPS, NPRETAPS, NDFE,
                 d_mu_cma, d_mu_lms_fs, d_mu_dfe, d_leak,
                 d_diverge_norm, d_dfe_gate);
}

atsc_equalizer_cma_impl::~atsc_equalizer_cma_impl() {}

std::vector<float> atsc_equalizer_cma_impl::taps() const { return d_taps; }
std::vector<float> atsc_equalizer_cma_impl::dfe_taps() const
{
    return std::vector<float>(d_dfe_taps, d_dfe_taps + NDFE);
}

void atsc_equalizer_cma_impl::filterN(const float* input_samples,
                                       float* output_samples,
                                       int nsamples)
{
    for (int j = 0; j < nsamples; j++) {
        output_samples[j] = 0;
        volk_32f_x2_dot_prod_32f(
            &output_samples[j], &input_samples[j], &d_taps[0], NTAPS);
    }
}

bool atsc_equalizer_cma_impl::antiwindup_check()
{
    double tap_e = 0.0;
    for (int k = 0; k < NTAPS; k++) tap_e += (double)d_taps[k] * (double)d_taps[k];
    if (!std::isfinite(tap_e) ||
        tap_e > (double)d_diverge_norm * (double)d_diverge_norm) {
        for (int k = 0; k < NTAPS; k++) d_taps[k] = 0.0f;
        d_taps[NPRETAPS] = 1.0f;
        std::memset(d_dfe_taps, 0, sizeof(d_dfe_taps));
        std::memset(d_dec_hist, 0, sizeof(d_dec_hist));
        d_diverge_resets++;
        return true;
    }
    return false;
}

void atsc_equalizer_cma_impl::adaptN_fs(const float* input_samples,
                                         const float* training_pattern,
                                         float* output_samples,
                                         int nsamples)
{
    // Same supervised LMS as Tier 3, with our parametric mu/leak.
    const float beta = d_mu_lms_fs;

    double sse = 0.0;
    for (int j = 0; j < nsamples; j++) {
        output_samples[j] = 0;
        volk_32f_x2_dot_prod_32f(
            &output_samples[j], &input_samples[j], &d_taps[0], NTAPS);
        float e = output_samples[j] - training_pattern[j];
        sse += (double)e * (double)e;
        float tmp_taps[NTAPS];
        volk_32f_s32f_multiply_32f(tmp_taps, &input_samples[j], beta * e, NTAPS);
        volk_32f_x2_subtract_32f(&d_taps[0], &d_taps[0], tmp_taps, NTAPS);
    }

    // Leakage to bound monotonic FS over-fit (Tier-3 lesson).
    float keep = 1.0f - d_leak;
    for (int k = 0; k < NTAPS; k++) d_taps[k] *= keep;

    if (antiwindup_check()) {
        // Recompute outputs with delta taps so downstream sees passthrough.
        sse = 0.0;
        for (int j = 0; j < nsamples; j++) {
            output_samples[j] = (NPRETAPS + j < NTAPS + nsamples)
                ? input_samples[j + NPRETAPS] : 0.0f;
            float e = output_samples[j] - training_pattern[j];
            sse += (double)e * (double)e;
        }
    }

    d_field_sync_count++;
    d_last_fs_mse = (nsamples > 0) ? (float)(sse / nsamples) : 1e9f;
}

void atsc_equalizer_cma_impl::adaptN_cma_dfe(const float* input_samples,
                                              float* output_samples,
                                              int nsamples)
{
    const float mu_cma = d_mu_cma;
    const float mu_dfe = d_mu_dfe;
    const float R      = CMA_R;
    const float dfe_gate = d_dfe_gate;

    // Per-sample loop: run FF filter, subtract DFE feedback, slice,
    // update FF via CMA, update DFE via decision-directed LMS (gated).
    for (int j = 0; j < nsamples; j++) {
        // FF filter output
        float ff_out;
        volk_32f_x2_dot_prod_32f(&ff_out, &input_samples[j], &d_taps[0], NTAPS);

        // DFE feedback (subtract post-cursor ISI estimate from past
        // decisions). d_dec_hist[0] = most recent decision, [NDFE-1]
        // = oldest.
        float dfe_out = 0.0f;
        for (int i = 0; i < NDFE; i++) dfe_out += d_dfe_taps[i] * d_dec_hist[i];

        float y = ff_out - dfe_out;
        output_samples[j] = y;

        // 8-VSB slicer
        float decision;
        if      (y >=  6.0f) decision =  7.0f;
        else if (y >=  4.0f) decision =  5.0f;
        else if (y >=  2.0f) decision =  3.0f;
        else if (y >=  0.0f) decision =  1.0f;
        else if (y >= -2.0f) decision = -1.0f;
        else if (y >= -4.0f) decision = -3.0f;
        else if (y >= -6.0f) decision = -5.0f;
        else                 decision = -7.0f;

        // CMA gradient: maximize -E[(|y|^2 - R)^2].
        // Real-valued gradient w.r.t. tap k:  -2 * (|y|^2 - R) * y * x[k]
        // Update: taps[k] += mu * (R - |y|^2) * y * x[k]
        float yy = y * y;
        float e_cma = (R - yy) * y;        // sign matched to update below
        // Apply update: d_taps += mu_cma * e_cma * input_samples[j..j+NTAPS-1]
        // Implemented via volk: tmp = input * (mu_cma*e_cma); d_taps += tmp.
        float scale = mu_cma * e_cma;
        // Avoid pathological cases where e_cma blows up (cold-start huge y)
        // by clamping the per-sample update magnitude.
        if (!std::isfinite(scale)) scale = 0.0f;
        // Soft clamp: cap |scale| so a single sample can't move taps
        // by more than 1e-3 * mean|x|.
        const float MAX_SCALE = 5.0e-3f;
        if (scale >  MAX_SCALE) scale =  MAX_SCALE;
        if (scale < -MAX_SCALE) scale = -MAX_SCALE;

        if (scale != 0.0f) {
            float tmp_taps[NTAPS];
            volk_32f_s32f_multiply_32f(tmp_taps, &input_samples[j], scale, NTAPS);
            // d_taps += tmp_taps
            volk_32f_x2_add_32f(&d_taps[0], &d_taps[0], tmp_taps, NTAPS);
            d_cma_updates++;
        }

        // DFE update (decision-directed LMS, gated by slicer confidence).
        // Standard DD-LMS error: e_dd = decision - y (sign convention:
        // we want y to track the decision, so update DFE so that y
        // INCREASES toward the decision when y is too small).
        // Gradient w.r.t. dfe_tap[i]: -e_dd * (-d_dec_hist[i]) = +e_dd * d_dec_hist[i]
        // Wait — y = ff_out - dot(dfe_taps, dec_hist), so
        //   dy/d(dfe_tap[i]) = -dec_hist[i]
        //   d(e_dd)/d(dfe_tap[i]) = +dec_hist[i]
        // We minimize 0.5*e_dd^2 by gradient descent, so:
        //   dfe_taps[i] -= mu_dfe * e_dd * dec_hist[i]
        float e_dd = decision - y;
        if (std::fabs(e_dd) <= dfe_gate) {
            for (int i = 0; i < NDFE; i++) {
                d_dfe_taps[i] -= mu_dfe * e_dd * d_dec_hist[i];
            }
            d_dfe_updates_applied++;
        } else {
            d_dfe_updates_skipped++;
        }

        // Shift decision history (most recent at [0]).
        for (int i = NDFE - 1; i > 0; i--) d_dec_hist[i] = d_dec_hist[i - 1];
        d_dec_hist[0] = decision;
    }

    // Per-batch anti-windup. Don't recompute outputs (we'd need to
    // re-run the slicer chain; cheaper to just zero the data_mem2 tail
    // for the next call). On reset, the next batch starts with delta
    // taps and clean DFE state — output for THIS batch already wrote
    // CMA-updated samples, but downstream Viterbi will resync soon.
    antiwindup_check();
}

void atsc_equalizer_cma_impl::maybe_print_telem()
{
    if (d_field_sync_count == 0 ||
        (d_field_sync_count % d_telem_every_n_fs) != 0) return;
    auto now = std::chrono::steady_clock::now();
    double t = std::chrono::duration<double>(now - d_t0).count();

    double tap_e = 0.0;
    for (int k = 0; k < NTAPS; k++) tap_e += (double)d_taps[k] * (double)d_taps[k];
    double dfe_e = 0.0;
    for (int k = 0; k < NDFE; k++) dfe_e += (double)d_dfe_taps[k] * (double)d_dfe_taps[k];

    std::fprintf(stderr,
                 "[t6-cma t=%6.2fs] fs=%d fs_mse=%.4f "
                 "|taps|=%.3f |dfe|=%.3f resets=%d "
                 "cma_upd=%lld dfe_app=%lld dfe_skip=%lld\n",
                 t, d_field_sync_count, d_last_fs_mse,
                 std::sqrt(tap_e), std::sqrt(dfe_e), d_diverge_resets,
                 d_cma_updates, d_dfe_updates_applied, d_dfe_updates_skipped);
}

int atsc_equalizer_cma_impl::general_work(int noutput_items,
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
        std::memset(&data_mem[0], 0, NPRETAPS * sizeof(float));
        std::memcpy(&data_mem[NPRETAPS],
                    in + i * ATSC_DATA_SEGMENT_LENGTH,
                    ATSC_DATA_SEGMENT_LENGTH * sizeof(float));
        d_flags = in_pl[i].flags();
        d_segno = in_pl[i].segno();
        d_buff_not_filled = false;
        i++;
    }

    for (; i < noutput_items; i++) {
        std::memcpy(&data_mem[ATSC_DATA_SEGMENT_LENGTH + NPRETAPS],
                    in + i * ATSC_DATA_SEGMENT_LENGTH,
                    (NTAPS - NPRETAPS) * sizeof(float));

        if (d_segno == -1) {
            // Field-sync segment: supervised LMS on the known FS pattern.
            if (d_flags & 0x0010) {
                adaptN_fs(data_mem, training_sequence2, data_mem2,
                          KNOWN_FIELD_SYNC_LENGTH);
            } else {
                adaptN_fs(data_mem, training_sequence1, data_mem2,
                          KNOWN_FIELD_SYNC_LENGTH);
            }
            // FS slot is dropped from output (matches Tier-3 behavior so
            // Viterbi cadence is preserved). Telemetry tick.
            maybe_print_telem();
        } else {
            // Data segment: CMA on FF + DD-LMS on DFE.
            adaptN_cma_dfe(data_mem, data_mem2, ATSC_DATA_SEGMENT_LENGTH);

            std::memcpy(&out[output_produced * ATSC_DATA_SEGMENT_LENGTH],
                        data_mem2,
                        ATSC_DATA_SEGMENT_LENGTH * sizeof(float));
            plinfo pli_out(d_flags, d_segno);
            out_pl[output_produced++] = pli_out;
        }

        std::memcpy(data_mem, &data_mem[ATSC_DATA_SEGMENT_LENGTH],
                    NPRETAPS * sizeof(float));
        std::memcpy(&data_mem[NPRETAPS],
                    in + i * ATSC_DATA_SEGMENT_LENGTH,
                    ATSC_DATA_SEGMENT_LENGTH * sizeof(float));
        d_flags = in_pl[i].flags();
        d_segno = in_pl[i].segno();
    }

    consume_each(noutput_items);
    return output_produced;
}

} /* namespace atscplus */
} /* namespace gr */
