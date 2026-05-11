/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Combined K-FS coherent LS + soft-confidence DD equalizer.
 */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_equalizer_pilot_multifs_dd_impl.h"
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

atsc_equalizer_pilot_multifs_dd::sptr atsc_equalizer_pilot_multifs_dd::make()
{
    return gnuradio::make_block_sptr<atsc_equalizer_pilot_multifs_dd_impl>();
}

static float bin_map(int bit) { return bit ? +5 : -5; }

static void init_field_sync_common(float* p, int mask)
{
    int i = 0;
    p[i++] = bin_map(1);
    p[i++] = bin_map(0);
    p[i++] = bin_map(0);
    p[i++] = bin_map(1);
    for (int j = 0; j < 511; j++) p[i++] = bin_map(atsc_pn511[j]);
    for (int j = 0; j < 63;  j++) p[i++] = bin_map(atsc_pn63[j]);
    for (int j = 0; j < 63;  j++) p[i++] = bin_map(atsc_pn63[j] ^ mask);
    for (int j = 0; j < 63;  j++) p[i++] = bin_map(atsc_pn63[j]);
}

static float env_f(const char* k, float dflt)
{
    if (const char* p = std::getenv(k)) { char* e = nullptr; float v = std::strtof(p, &e); if (e != p) return v; }
    return dflt;
}
static int env_i(const char* k, int dflt)
{
    if (const char* p = std::getenv(k)) { char* e = nullptr; long v = std::strtol(p, &e, 10); if (e != p) return (int)v; }
    return dflt;
}

static inline float slice_vsb(float y)
{
    float a = std::fabs(y);
    int n = (int)std::lround((a - 1.0f) * 0.5f);
    if (n < 0) n = 0;
    if (n > 3) n = 3;
    int out_abs = 2 * n + 1;
    return y >= 0 ? (float)out_abs : -(float)out_abs;
}

atsc_equalizer_pilot_multifs_dd_impl::atsc_equalizer_pilot_multifs_dd_impl()
    : gr::block("atsc_equalizer_pilot_multifs_dd",
                io_signature::make2(2, 2, ATSC_DATA_SEGMENT_LENGTH * sizeof(float), sizeof(plinfo)),
                io_signature::make2(2, 2, ATSC_DATA_SEGMENT_LENGTH * sizeof(float), sizeof(plinfo)))
{
    init_field_sync_common(training_sequence1, 0);
    init_field_sync_common(training_sequence2, 1);

    d_taps.resize(NTAPS, 0.0f);
    d_taps[NPRETAPS] = 1.0f;
    d_taps_lastsolve = d_taps;

    d_K     = env_i("PILOT_MFSDD_K",     4);
    d_ridge = env_f("PILOT_MFSDD_RIDGE", 1e-2f);
    d_mu    = env_f("PILOT_MFSDD_MU",    5e-5f);
    d_gate  = env_f("PILOT_MFSDD_GATE",  1.0f);
    d_debug = env_i("PILOT_MFSDD_DEBUG", 0);
    if (d_K < 1) d_K = 1;
    if (d_K > 64) d_K = 64;
    d_inv_gate = (d_gate > 1e-6f) ? (1.0f / d_gate) : 1.0f;

    d_AtA = Eigen::MatrixXd::Zero(NTAPS, NTAPS);
    d_Atb = Eigen::VectorXd::Zero(NTAPS);
    d_fs_in_window = 0;

    d_n_fs = d_n_solves = d_n_data_segs = 0;
    d_n_dd_active = d_n_dd_zero = 0;
    d_sum_conf = 0.0;
    d_n_dd_diverge = 0;

    std::fprintf(stderr,
        "[pilot_mfsdd] K=%d ridge=%.2g mu=%.2g gate=%.2f\n",
        d_K, d_ridge, d_mu, d_gate);

    const int alignment_multiple = volk_get_alignment() / sizeof(float);
    set_alignment(std::max(1, alignment_multiple));
}

atsc_equalizer_pilot_multifs_dd_impl::~atsc_equalizer_pilot_multifs_dd_impl()
{
    long total = d_n_dd_active + d_n_dd_zero;
    double mean_conf = (total > 0) ? d_sum_conf / (double)total : 0.0;
    std::fprintf(stderr,
        "[pilot_mfsdd FINAL] fs=%ld solves=%ld data_segs=%ld dd_active=%ld dd_zero=%ld mean_conf=%.4f dd_diverge=%ld\n",
        d_n_fs, d_n_solves, d_n_data_segs, d_n_dd_active, d_n_dd_zero, mean_conf, d_n_dd_diverge);
}

std::vector<float> atsc_equalizer_pilot_multifs_dd_impl::taps() const { return d_taps; }
std::vector<float> atsc_equalizer_pilot_multifs_dd_impl::data() const
{
    return std::vector<float>(&data_mem2[0], &data_mem2[ATSC_DATA_SEGMENT_LENGTH - 1]);
}
float atsc_equalizer_pilot_multifs_dd_impl::last_residual_rms() const { return d_last_residual_rms; }

void atsc_equalizer_pilot_multifs_dd_impl::filterN(const float* input_samples, float* output_samples, int nsamples)
{
    for (int j = 0; j < nsamples; j++) {
        output_samples[j] = 0;
        volk_32f_x2_dot_prod_32f(&output_samples[j], &input_samples[j], &d_taps[0], NTAPS);
    }
}

void atsc_equalizer_pilot_multifs_dd_impl::accumulate_FS(const float* input_samples, const float* training_pattern)
{
    static constexpr int N = NTAPS;
    static constexpr int M = KNOWN_FIELD_SYNC_LENGTH;
    for (int j = 0; j < M; j++) {
        const float* row = &input_samples[j];
        const double bj = training_pattern[j];
        for (int i = 0; i < N; i++) {
            const double xi = row[i];
            d_Atb(i) += xi * bj;
            for (int k = i; k < N; k++) {
                d_AtA(i, k) += xi * (double)row[k];
            }
        }
    }
}

void atsc_equalizer_pilot_multifs_dd_impl::solve_taps_LS()
{
    static constexpr int N = NTAPS;
    static constexpr float DIVERGENCE_BAIL = 50.0f;

    Eigen::MatrixXd AtA = d_AtA;
    Eigen::VectorXd Atb = d_Atb;
    for (int i = 0; i < N; i++) {
        for (int k = 0; k < i; k++) AtA(i, k) = AtA(k, i);
        AtA(i, i) += d_ridge;
    }
    Atb(NPRETAPS) += (double)d_ridge * 1.0;

    Eigen::LDLT<Eigen::MatrixXd> ldlt(AtA);
    if (ldlt.info() != Eigen::Success) {
        d_last_residual_rms = -1.0f;
        d_AtA.setZero(); d_Atb.setZero();
        return;
    }
    Eigen::VectorXd h = ldlt.solve(Atb);

    double tap_e = 0.0;
    for (int k = 0; k < N; k++) tap_e += h(k) * h(k);
    if (!std::isfinite(tap_e) || tap_e > (double)DIVERGENCE_BAIL * DIVERGENCE_BAIL) {
        for (int k = 0; k < N; k++) d_taps[k] = 0.0f;
        d_taps[NPRETAPS] = 1.0f;
        d_taps_lastsolve = d_taps;
        d_last_residual_rms = -2.0f;
        d_AtA.setZero(); d_Atb.setZero();
        return;
    }

    double dev = 0.0;
    for (int k = 0; k < N; k++) {
        double d = h(k) - (k == NPRETAPS ? 1.0 : 0.0);
        dev += d * d;
    }
    d_last_residual_rms = (float)std::sqrt(dev / N);

    for (int k = 0; k < N; k++) d_taps[k] = (float)h(k);
    d_taps_lastsolve = d_taps;

    if (d_debug) {
        std::fprintf(stderr,
            "[pilot_mfsdd] solve#%ld K=%d dev_rms=%.3f\n",
            d_n_solves, d_K, d_last_residual_rms);
    }
    d_AtA.setZero(); d_Atb.setZero();
}

void atsc_equalizer_pilot_multifs_dd_impl::filter_and_softdd(const float* input_samples, float* output_samples, int nsamples)
{
    static constexpr float DIVERGENCE_BAIL = 50.0f;
    const float mu = d_mu;
    const float inv_gate = d_inv_gate;
    long n_active = 0, n_zero = 0;
    double sum_conf = 0.0;

    for (int j = 0; j < nsamples; j++) {
        float y = 0.0f;
        volk_32f_x2_dot_prod_32f(&y, &input_samples[j], &d_taps[0], NTAPS);
        output_samples[j] = y;

        if (mu <= 0.0f) continue;

        float s = slice_vsb(y);
        float e = y - s;
        float ae = std::fabs(e);
        float conf = 1.0f - ae * inv_gate;
        if (conf <= 0.0f) {
            n_zero++;
            continue;
        }
        n_active++;
        sum_conf += conf;

        float scale = mu * conf * e;
        float tmp[NTAPS];
        volk_32f_s32f_multiply_32f(tmp, &input_samples[j], scale, NTAPS);
        volk_32f_x2_subtract_32f(&d_taps[0], &d_taps[0], tmp, NTAPS);
    }

    d_n_dd_active += n_active;
    d_n_dd_zero += n_zero;
    d_sum_conf += sum_conf;

    double tap_e = 0.0;
    for (int k = 0; k < NTAPS; k++) tap_e += (double)d_taps[k] * d_taps[k];
    if (!std::isfinite(tap_e) || tap_e > (double)DIVERGENCE_BAIL * DIVERGENCE_BAIL) {
        if ((int)d_taps_lastsolve.size() == NTAPS) {
            std::memcpy(&d_taps[0], &d_taps_lastsolve[0], NTAPS * sizeof(float));
        } else {
            for (int k = 0; k < NTAPS; k++) d_taps[k] = 0.0f;
            d_taps[NPRETAPS] = 1.0f;
        }
        d_n_dd_diverge++;
    }
}

int atsc_equalizer_pilot_multifs_dd_impl::general_work(int noutput_items,
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
            const float* train = (d_flags & 0x0010)
                ? training_sequence2
                : training_sequence1;
            accumulate_FS(data_mem, train);
            d_n_fs++;
            d_fs_in_window++;
            if (d_fs_in_window >= d_K) {
                solve_taps_LS();
                d_n_solves++;
                d_fs_in_window = 0;
            }
        } else {
            filter_and_softdd(data_mem, data_mem2, ATSC_DATA_SEGMENT_LENGTH);

            memcpy(&out[output_produced * ATSC_DATA_SEGMENT_LENGTH],
                   data_mem2,
                   ATSC_DATA_SEGMENT_LENGTH * sizeof(float));

            plinfo pli_out(d_flags, d_segno);
            out_pl[output_produced++] = pli_out;
            d_n_data_segs++;
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

} /* namespace atscplus */
} /* namespace gr */
