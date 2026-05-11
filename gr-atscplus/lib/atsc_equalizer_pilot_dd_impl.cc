/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Pilot-LS + Decision-Directed equalizer. See
 * ~/overnight/BC_COMBINED_RESULT.md for design rationale.
 */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_equalizer_pilot_dd_impl.h"
#include "atsc_pnXXX_impl.h"
#include "atsc_types.h"
#include <gnuradio/io_signature.h>
#include <volk/volk.h>
#include <Eigen/Dense>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace gr {
namespace atscplus {
using gr::dtv::plinfo;
using gr::dtv::ATSC_DATA_SEGMENT_LENGTH;

atsc_equalizer_pilot_dd::sptr atsc_equalizer_pilot_dd::make()
{
    return gnuradio::make_block_sptr<atsc_equalizer_pilot_dd_impl>();
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
    if (const char* p = std::getenv(k)) {
        char* end = nullptr;
        float v = std::strtof(p, &end);
        if (end != p) return v;
    }
    return dflt;
}
static int env_i(const char* k, int dflt)
{
    if (const char* p = std::getenv(k)) {
        char* end = nullptr;
        long v = std::strtol(p, &end, 10);
        if (end != p) return (int)v;
    }
    return dflt;
}

atsc_equalizer_pilot_dd_impl::atsc_equalizer_pilot_dd_impl()
    : gr::block("atsc_equalizer_pilot_dd",
                io_signature::make2(
                    2, 2, ATSC_DATA_SEGMENT_LENGTH * sizeof(float), sizeof(plinfo)),
                io_signature::make2(
                    2, 2, ATSC_DATA_SEGMENT_LENGTH * sizeof(float), sizeof(plinfo)))
{
    init_field_sync_common(training_sequence1, 0);
    init_field_sync_common(training_sequence2, 1);

    d_taps.resize(NTAPS, 0.0f);
    d_taps[NPRETAPS] = 1.0f;
    d_taps_lastfs = d_taps;

    d_mu       = env_f("PILOT_DD_MU",       3e-4f);
    d_gate     = env_f("PILOT_DD_GATE",     2.0f);
    d_leak     = env_f("PILOT_DD_LEAK",     0.0f);
    d_reset_fs = env_i("PILOT_DD_RESET_FS", 1);
    d_ridge    = env_f("PILOT_DD_RIDGE",    1e-2f);
    d_debug    = env_i("PILOT_DD_DEBUG",    0);

    d_n_fs = d_n_data_segs = 0;
    d_n_dd_updates = d_n_dd_skipped = d_n_dd_diverge = d_n_dd_resets = 0;

    if (d_debug) {
        std::fprintf(stderr,
            "[pilot_dd] mu=%.2g gate=%.2f leak=%.2g reset_fs=%d ridge=%.2g\n",
            d_mu, d_gate, d_leak, d_reset_fs, d_ridge);
    }

    const int alignment_multiple = volk_get_alignment() / sizeof(float);
    set_alignment(std::max(1, alignment_multiple));
}

atsc_equalizer_pilot_dd_impl::~atsc_equalizer_pilot_dd_impl()
{
    std::fprintf(stderr,
        "[pilot_dd FINAL] fs=%ld data_segs=%ld dd_upd=%ld dd_gated=%ld "
        "dd_diverge=%ld fs_resets=%ld\n",
        d_n_fs, d_n_data_segs, d_n_dd_updates, d_n_dd_skipped,
        d_n_dd_diverge, d_n_dd_resets);
}

std::vector<float> atsc_equalizer_pilot_dd_impl::taps() const { return d_taps; }
std::vector<float> atsc_equalizer_pilot_dd_impl::data() const
{
    return std::vector<float>(&data_mem2[0], &data_mem2[ATSC_DATA_SEGMENT_LENGTH - 1]);
}
float atsc_equalizer_pilot_dd_impl::last_residual_rms() const { return d_last_residual_rms; }

void atsc_equalizer_pilot_dd_impl::filterN(const float* input_samples,
                                            float* output_samples,
                                            int nsamples)
{
    for (int j = 0; j < nsamples; j++) {
        output_samples[j] = 0;
        volk_32f_x2_dot_prod_32f(
            &output_samples[j], &input_samples[j], &d_taps[0], NTAPS);
    }
}

// 8-VSB slicer. Maps to nearest of {-7,-5,-3,-1,+1,+3,+5,+7}.
static inline float slice_vsb(float y)
{
    float a = std::fabs(y);
    int q = (int)std::floor(a) | 1;        // odd: 1,3,5,7,...
    if (q < 1) q = 1;
    if (q > 7) q = 7;
    // For values like 3.6, floor=3 (odd) -> q=3 (giving 3 vs 5). Better:
    // round to nearest odd. nearest odd of a = 2*round((a-1)/2)+1, clamped.
    int n = (int)std::lround((a - 1.0f) * 0.5f);
    if (n < 0) n = 0;
    if (n > 3) n = 3;
    int out_abs = 2 * n + 1;
    return y >= 0 ? (float)out_abs : -(float)out_abs;
}

// Filter + DD-LMS update on each emitted symbol, with gating + leakage + bail.
void atsc_equalizer_pilot_dd_impl::filter_and_dd_update(const float* input_samples,
                                                         float* output_samples,
                                                         int nsamples)
{
    static constexpr float DIVERGENCE_BAIL = 50.0f;

    const float mu = d_mu;
    const float gate = d_gate;
    long n_upd = 0, n_gat = 0;

    for (int j = 0; j < nsamples; j++) {
        // Filter this output.
        float y = 0.0f;
        volk_32f_x2_dot_prod_32f(&y, &input_samples[j], &d_taps[0], NTAPS);
        output_samples[j] = y;

        if (mu <= 0.0f) continue;

        float s = slice_vsb(y);
        float e = y - s;

        if (std::fabs(e) > gate) { n_gat++; continue; }

        // h <- h - mu * e * x
        float scale = mu * e;
        float tmp[NTAPS];
        volk_32f_s32f_multiply_32f(tmp, &input_samples[j], scale, NTAPS);
        volk_32f_x2_subtract_32f(&d_taps[0], &d_taps[0], tmp, NTAPS);
        n_upd++;
    }

    d_n_dd_updates += n_upd;
    d_n_dd_skipped += n_gat;

    if (d_leak > 0.0f) {
        float keep = 1.0f - d_leak;
        for (int k = 0; k < NTAPS; k++) d_taps[k] *= keep;
    }

    // Divergence bail.
    double tap_e = 0.0;
    for (int k = 0; k < NTAPS; k++) tap_e += (double)d_taps[k] * d_taps[k];
    if (!std::isfinite(tap_e) || tap_e > (double)DIVERGENCE_BAIL * DIVERGENCE_BAIL) {
        // Restore last FS LS solution (or delta if unavailable).
        if ((int)d_taps_lastfs.size() == NTAPS) {
            std::memcpy(&d_taps[0], &d_taps_lastfs[0], NTAPS * sizeof(float));
        } else {
            for (int k = 0; k < NTAPS; k++) d_taps[k] = 0.0f;
            d_taps[NPRETAPS] = 1.0f;
        }
        d_n_dd_diverge++;
    }
}

// Same closed-form LS as atsc_equalizer_pilot.
void atsc_equalizer_pilot_dd_impl::estimate_taps_LS(const float* input_samples,
                                                     const float* training_pattern)
{
    static constexpr int N = NTAPS;
    static constexpr int M = KNOWN_FIELD_SYNC_LENGTH;
    static constexpr float DIVERGENCE_BAIL = 50.0f;

    float RIDGE = d_ridge;

    Eigen::MatrixXd AtA = Eigen::MatrixXd::Zero(N, N);
    Eigen::VectorXd Atb = Eigen::VectorXd::Zero(N);

    for (int j = 0; j < M; j++) {
        const float* row = &input_samples[j];
        const double bj = training_pattern[j];
        for (int i = 0; i < N; i++) {
            const double xi = row[i];
            Atb(i) += xi * bj;
            for (int k = i; k < N; k++) {
                AtA(i, k) += xi * (double)row[k];
            }
        }
    }
    for (int i = 0; i < N; i++) {
        for (int k = 0; k < i; k++) AtA(i, k) = AtA(k, i);
        AtA(i, i) += RIDGE;
    }
    Atb(NPRETAPS) += (double)RIDGE * 1.0;

    Eigen::LDLT<Eigen::MatrixXd> ldlt(AtA);
    Eigen::VectorXd h;
    if (ldlt.info() == Eigen::Success) {
        h = ldlt.solve(Atb);
    } else {
        d_last_residual_rms = -1.0f;
        return;
    }

    double tap_e = 0.0;
    for (int k = 0; k < N; k++) tap_e += h(k) * h(k);
    if (!std::isfinite(tap_e) || tap_e > (double)DIVERGENCE_BAIL * DIVERGENCE_BAIL) {
        for (int k = 0; k < N; k++) d_taps[k] = 0.0f;
        d_taps[NPRETAPS] = 1.0f;
        d_taps_lastfs = d_taps;
        d_last_residual_rms = -2.0f;
        return;
    }

    double sse = 0.0;
    for (int j = 0; j < M; j++) {
        double y = 0.0;
        for (int k = 0; k < N; k++) y += h(k) * (double)input_samples[j + k];
        double e = y - (double)training_pattern[j];
        sse += e * e;
    }
    d_last_residual_rms = (float)std::sqrt(sse / (double)M);

    for (int k = 0; k < N; k++) d_taps[k] = (float)h(k);
    // Snapshot for DD divergence-bail and (optional) per-FS restore.
    d_taps_lastfs = d_taps;

    if (d_debug) {
        double max_abs = 0.0; int max_idx = -1;
        for (int k = 0; k < N; k++) {
            double a = std::abs(h(k));
            if (a > max_abs) { max_abs = a; max_idx = k; }
        }
        std::fprintf(stderr,
            "[pilot_dd] FS#%ld resid_rms=%.3f tap_e=%.3f peak_tap=%d val=%+.4f\n",
            d_n_fs, d_last_residual_rms, std::sqrt(tap_e), max_idx,
            (float)h(max_idx));
    }
}

int atsc_equalizer_pilot_dd_impl::general_work(int noutput_items,
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
            estimate_taps_LS(data_mem, train);
            d_n_fs++;
            if (d_reset_fs) d_n_dd_resets++;
            // FS not emitted on stream 0.
        } else {
            // DD: filter AND adapt in one pass.
            filter_and_dd_update(data_mem, data_mem2, ATSC_DATA_SEGMENT_LENGTH);

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
