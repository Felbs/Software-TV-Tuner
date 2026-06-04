/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Pilot-based ATSC channel equalizer: closed-form ridge LS solve per
 * Field Sync, replacing LMS gradient descent. See
 * ~/overnight/TASK3_EQ_REPLACEMENT_SCOPE.md for design rationale.
 */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_equalizer_pilot_impl.h"
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

atsc_equalizer_pilot::sptr atsc_equalizer_pilot::make()
{
    return gnuradio::make_block_sptr<atsc_equalizer_pilot_impl>();
}

static float bin_map(int bit) { return bit ? +5 : -5; }

static void init_field_sync_common(float* p, int mask)
{
    int i = 0;
    p[i++] = bin_map(1); // data segment sync pulse
    p[i++] = bin_map(0);
    p[i++] = bin_map(0);
    p[i++] = bin_map(1);
    for (int j = 0; j < 511; j++) p[i++] = bin_map(atsc_pn511[j]);
    for (int j = 0; j < 63;  j++) p[i++] = bin_map(atsc_pn63[j]);
    for (int j = 0; j < 63;  j++) p[i++] = bin_map(atsc_pn63[j] ^ mask);
    for (int j = 0; j < 63;  j++) p[i++] = bin_map(atsc_pn63[j]);
}

atsc_equalizer_pilot_impl::atsc_equalizer_pilot_impl()
    : gr::block("atsc_equalizer_pilot",
                io_signature::make2(
                    2, 2, ATSC_DATA_SEGMENT_LENGTH * sizeof(float), sizeof(plinfo)),
                io_signature::make2(
                    2, 2, ATSC_DATA_SEGMENT_LENGTH * sizeof(float), sizeof(plinfo)))
{
    init_field_sync_common(training_sequence1, 0);
    init_field_sync_common(training_sequence2, 1);

    d_taps.resize(NTAPS, 0.0f);
    d_taps[NPRETAPS] = 1.0f; // delta init for cold-start data segments before first FS

    const int alignment_multiple = volk_get_alignment() / sizeof(float);
    set_alignment(std::max(1, alignment_multiple));
}

atsc_equalizer_pilot_impl::~atsc_equalizer_pilot_impl() {}

std::vector<float> atsc_equalizer_pilot_impl::taps() const { return d_taps; }

std::vector<float> atsc_equalizer_pilot_impl::data() const
{
    std::vector<float> ret(&data_mem2[0], &data_mem2[ATSC_DATA_SEGMENT_LENGTH - 1]);
    return ret;
}

float atsc_equalizer_pilot_impl::last_residual_rms() const { return d_last_residual_rms; }

void atsc_equalizer_pilot_impl::filterN(const float* input_samples,
                                        float* output_samples,
                                        int nsamples)
{
    for (int j = 0; j < nsamples; j++) {
        output_samples[j] = 0;
        volk_32f_x2_dot_prod_32f(
            &output_samples[j], &input_samples[j], &d_taps[0], NTAPS);
    }
}

// Closed-form ridge LS solve: (A^T A + λI) h = A^T b
// A is (TRAIN_LEN × NTAPS), A[j,k] = input_samples[j + k]
// b is the known training pattern (TRAIN_LEN-vector, ±5).
// Result h replaces d_taps.
void atsc_equalizer_pilot_impl::estimate_taps_LS(const float* input_samples,
                                                  const float* training_pattern)
{
    static constexpr int N = NTAPS;
    static constexpr int M = KNOWN_FIELD_SYNC_LENGTH;
    static constexpr float DIVERGENCE_BAIL = 50.0f;
    // Ridge λ. Default 1e-2 → near-zero regularization (good fit but
    // can over-fit noise into satellite taps). Override at runtime via
    // env var PILOT_RIDGE for tuning. AtA diagonal is roughly
    // M * input_var ≈ 704 * 25 ≈ 1.8e4, so ridge values up to ~1e3 are
    // mild compared to data Fisher info; >1e4 dominates.
    float RIDGE = 1e-2f;
    if (const char* p = std::getenv("PILOT_RIDGE")) {
        float v = std::atof(p);
        if (v > 0.0f) RIDGE = v;
    }

    // Build A^T A (NxN, symmetric) and A^T b (Nx1) directly without
    // materializing A. AtA[i,k] = sum_{j=0..M-1} input[j+i] * input[j+k].
    // Heap-allocated (MatrixXd) to avoid blowing the stack at N=256.
    Eigen::MatrixXd AtA = Eigen::MatrixXd::Zero(N, N);
    Eigen::VectorXd Atb = Eigen::VectorXd::Zero(N);

    for (int j = 0; j < M; j++) {
        const float* row = &input_samples[j];
        const double bj = training_pattern[j];
        for (int i = 0; i < N; i++) {
            const double xi = row[i];
            Atb(i) += xi * bj;
            // Only fill upper triangle; copy down after.
            for (int k = i; k < N; k++) {
                AtA(i, k) += xi * (double)row[k];
            }
        }
    }
    for (int i = 0; i < N; i++) {
        for (int k = 0; k < i; k++) AtA(i, k) = AtA(k, i);
        AtA(i, i) += RIDGE;
    }
    // Regularize toward delta init (h = δ at NPRETAPS) rather than h = 0.
    // ||A h - b||^2 + λ ||h - δ||^2 → A^T A + λI same; rhs becomes A^T b + λ δ.
    // Without this, ridge pulls all taps to zero — including the main tap.
    Atb(NPRETAPS) += (double)RIDGE * 1.0;

    Eigen::LDLT<Eigen::MatrixXd> ldlt(AtA);
    Eigen::VectorXd h;
    if (ldlt.info() == Eigen::Success) {
        h = ldlt.solve(Atb);
    } else {
        // Numerical failure: keep prior taps.
        d_last_residual_rms = -1.0f;
        return;
    }

    // Sanity check: tap energy.
    double tap_e = 0.0;
    for (int k = 0; k < N; k++) tap_e += h(k) * h(k);
    if (!std::isfinite(tap_e) || tap_e > (double)DIVERGENCE_BAIL * DIVERGENCE_BAIL) {
        // Reset to delta on divergence.
        for (int k = 0; k < N; k++) d_taps[k] = 0.0f;
        d_taps[NPRETAPS] = 1.0f;
        d_last_residual_rms = -2.0f;
        return;
    }

    // Compute training residual RMS (for diagnostics).
    double sse = 0.0;
    for (int j = 0; j < M; j++) {
        double y = 0.0;
        for (int k = 0; k < N; k++) y += h(k) * (double)input_samples[j + k];
        double e = y - (double)training_pattern[j];
        sse += e * e;
    }
    d_last_residual_rms = (float)std::sqrt(sse / (double)M);

    for (int k = 0; k < N; k++) d_taps[k] = (float)h(k);

    if (std::getenv("PILOT_DEBUG")) {
        // Print headline diagnostics: residual RMS, tap energy, peak tap idx.
        double max_abs = 0.0; int max_idx = -1;
        for (int k = 0; k < N; k++) {
            double a = std::abs(h(k));
            if (a > max_abs) { max_abs = a; max_idx = k; }
        }
        // Energy in input training window (for context).
        double in_e = 0.0; int in_n = M + N - 1;
        for (int k = 0; k < in_n; k++) in_e += (double)input_samples[k] * input_samples[k];
        double in_rms = std::sqrt(in_e / (double)in_n);
        std::fprintf(stderr,
            "[pilot_eq] resid_rms=%.3f tap_e=%.3f peak_tap=%d val=%+.4f in_rms=%.3f\n",
            d_last_residual_rms, std::sqrt(tap_e), max_idx,
            (float)h(max_idx), in_rms);
    }
}

int atsc_equalizer_pilot_impl::general_work(int noutput_items,
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
            // FS segment is consumed; not emitted on stream 0.
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

} /* namespace atscplus */
} /* namespace gr */
