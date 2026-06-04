/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Pilot-LS multi-FS-coherent equalizer.
 *
 * Same closed-form ridge LS as atsc_equalizer_pilot, but the normal
 * equations (AtA, Atb) are accumulated across K successive Field Syncs
 * before the LDLT solve. Tap variance scales as 1/K when channel is
 * static across the K-FS window. K=4 gives 4x lower tap noise at the
 * cost of one tap update per ~96.8 ms (vs every 24.2 ms for K=1).
 *
 * Between solves the equalizer simply filters with the most-recently
 * solved taps — no DD adaptation. (For DD-on-top behavior, layer
 * pilot_dd_soft after this block, but typical use is solo.)
 */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_equalizer_pilot_multifs_impl.h"
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

atsc_equalizer_pilot_multifs::sptr atsc_equalizer_pilot_multifs::make()
{
    return gnuradio::make_block_sptr<atsc_equalizer_pilot_multifs_impl>();
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

atsc_equalizer_pilot_multifs_impl::atsc_equalizer_pilot_multifs_impl()
    : gr::block("atsc_equalizer_pilot_multifs",
                io_signature::make2(
                    2, 2, ATSC_DATA_SEGMENT_LENGTH * sizeof(float), sizeof(plinfo)),
                io_signature::make2(
                    2, 2, ATSC_DATA_SEGMENT_LENGTH * sizeof(float), sizeof(plinfo)))
{
    init_field_sync_common(training_sequence1, 0);
    init_field_sync_common(training_sequence2, 1);

    d_taps.resize(NTAPS, 0.0f);
    d_taps[NPRETAPS] = 1.0f;

    d_K     = env_i("PILOT_MULTIFS_K",     4);
    d_ridge = env_f("PILOT_MULTIFS_RIDGE", 1e-2f);
    d_debug = env_i("PILOT_MULTIFS_DEBUG", 0);
    if (d_K < 1) d_K = 1;
    if (d_K > 64) d_K = 64;

    d_AtA = Eigen::MatrixXd::Zero(NTAPS, NTAPS);
    d_Atb = Eigen::VectorXd::Zero(NTAPS);
    d_fs_in_window = 0;

    d_n_fs = d_n_solves = d_n_data_segs = 0;

    std::fprintf(stderr,
        "[pilot_multifs] K=%d ridge=%.2g\n", d_K, d_ridge);

    const int alignment_multiple = volk_get_alignment() / sizeof(float);
    set_alignment(std::max(1, alignment_multiple));
}

atsc_equalizer_pilot_multifs_impl::~atsc_equalizer_pilot_multifs_impl()
{
    std::fprintf(stderr,
        "[pilot_multifs FINAL] fs=%ld solves=%ld data_segs=%ld\n",
        d_n_fs, d_n_solves, d_n_data_segs);
}

std::vector<float> atsc_equalizer_pilot_multifs_impl::taps() const { return d_taps; }
std::vector<float> atsc_equalizer_pilot_multifs_impl::data() const
{
    return std::vector<float>(&data_mem2[0], &data_mem2[ATSC_DATA_SEGMENT_LENGTH - 1]);
}
float atsc_equalizer_pilot_multifs_impl::last_residual_rms() const { return d_last_residual_rms; }

void atsc_equalizer_pilot_multifs_impl::filterN(const float* input_samples,
                                                  float* output_samples,
                                                  int nsamples)
{
    for (int j = 0; j < nsamples; j++) {
        output_samples[j] = 0;
        volk_32f_x2_dot_prod_32f(
            &output_samples[j], &input_samples[j], &d_taps[0], NTAPS);
    }
}

// Accumulate one FS's contribution into d_AtA / d_Atb. No solve yet.
void atsc_equalizer_pilot_multifs_impl::accumulate_FS(const float* input_samples,
                                                        const float* training_pattern)
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

// Solve and reset accumulators. Apply ridge here (single ridge for the K-FS solve).
void atsc_equalizer_pilot_multifs_impl::solve_taps_LS()
{
    static constexpr int N = NTAPS;
    static constexpr float DIVERGENCE_BAIL = 50.0f;

    Eigen::MatrixXd AtA = d_AtA;  // copy so we can keep accumulators or zero them
    Eigen::VectorXd Atb = d_Atb;

    // Symmetrize and add ridge. Atb shifted toward delta init at NPRETAPS.
    for (int i = 0; i < N; i++) {
        for (int k = 0; k < i; k++) AtA(i, k) = AtA(k, i);
        AtA(i, i) += d_ridge;
    }
    Atb(NPRETAPS) += (double)d_ridge * 1.0;

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
        d_last_residual_rms = -2.0f;
        return;
    }

    // Residual reported on the LAST FS in the window (cheap; we stored the
    // most-recent FS in data_mem when we accumulated it). Use Atb-derived
    // estimate if needed. For now, compute on the accumulated bilinear:
    //   sse = sum (b_j - h^T x_j)^2 = b^T b - 2 h^T (A^T b) + h^T (A^T A) h
    // We don't have b^T b stored, so estimate post-fit RMS as
    //   sse_est = h^T (AtA pre-ridge) h - 2 h^T (Atb) + (something)
    // Just report a coarse proxy: the L2 norm of h - delta.
    double dev = 0.0;
    for (int k = 0; k < N; k++) {
        double d = h(k) - (k == NPRETAPS ? 1.0 : 0.0);
        dev += d * d;
    }
    d_last_residual_rms = (float)std::sqrt(dev / N);

    for (int k = 0; k < N; k++) d_taps[k] = (float)h(k);

    if (d_debug) {
        double max_abs = 0.0; int max_idx = -1;
        for (int k = 0; k < N; k++) {
            double a = std::abs(h(k));
            if (a > max_abs) { max_abs = a; max_idx = k; }
        }
        std::fprintf(stderr,
            "[pilot_multifs] solve#%ld K=%d window peak_tap=%d val=%+.4f dev_rms=%.3f\n",
            d_n_solves, d_K, max_idx, (float)h(max_idx), d_last_residual_rms);
    }

    // Reset accumulators for next window.
    d_AtA.setZero();
    d_Atb.setZero();
}

int atsc_equalizer_pilot_multifs_impl::general_work(int noutput_items,
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
            // FS not emitted on stream 0.
        } else {
            // Filter only — no per-segment adaptation.
            filterN(data_mem, data_mem2, ATSC_DATA_SEGMENT_LENGTH);

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
