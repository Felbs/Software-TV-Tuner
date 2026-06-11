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
#include <chrono>
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
#if defined(__ARM_NEON) || defined(__aarch64__)
    static const bool S16 = []() {
        const char* p = std::getenv("STVT_EQ_S16");
        return p && std::atoi(p) != 0;
    }();
    if (S16) {
        filterN_s16(input_samples, output_samples, nsamples);
        return;
    }
#endif
    for (int j = 0; j < nsamples; j++) {
        output_samples[j] = 0;
        volk_32f_x2_dot_prod_32f(
            &output_samples[j], &input_samples[j], &d_taps[0], NTAPS);
    }
}

#if defined(__ARM_NEON) || defined(__aarch64__)
#include <arm_neon.h>

// Register-blocked int16 FIR: 4 outputs per pass, taps loaded once and shared,
// shifted input windows formed with vext (registers, not loads). See header.
static inline void eq_fir_s16_x4(const int16_t* x, const int16_t* t, int ntaps,
                                 int32_t* out)
{
    int32x4_t a0 = vdupq_n_s32(0), a1 = vdupq_n_s32(0),
              a2 = vdupq_n_s32(0), a3 = vdupq_n_s32(0);
    int16x8_t xv0 = vld1q_s16(x);
    for (int i = 0; i < ntaps; i += 8) {
        int16x8_t tv  = vld1q_s16(t + i);
        int16x8_t xv1 = vld1q_s16(x + i + 8);
        int16x8_t s1 = vextq_s16(xv0, xv1, 1);
        int16x8_t s2 = vextq_s16(xv0, xv1, 2);
        int16x8_t s3 = vextq_s16(xv0, xv1, 3);
        a0 = vmlal_s16(a0, vget_low_s16(xv0),  vget_low_s16(tv));
        a0 = vmlal_s16(a0, vget_high_s16(xv0), vget_high_s16(tv));
        a1 = vmlal_s16(a1, vget_low_s16(s1),   vget_low_s16(tv));
        a1 = vmlal_s16(a1, vget_high_s16(s1),  vget_high_s16(tv));
        a2 = vmlal_s16(a2, vget_low_s16(s2),   vget_low_s16(tv));
        a2 = vmlal_s16(a2, vget_high_s16(s2),  vget_high_s16(tv));
        a3 = vmlal_s16(a3, vget_low_s16(s3),   vget_low_s16(tv));
        a3 = vmlal_s16(a3, vget_high_s16(s3),  vget_high_s16(tv));
        xv0 = xv1;
    }
    out[0] = vaddvq_s32(a0);
    out[1] = vaddvq_s32(a1);
    out[2] = vaddvq_s32(a2);
    out[3] = vaddvq_s32(a3);
}

void atsc_equalizer_long_impl::filterN_s16(const float* input_samples,
                                           float* output_samples,
                                           int nsamples)
{
    // Q10 input (8-VSB levels ±7, headroom to ±31), Q11 taps (headroom to ±15).
    // Worst-case int32 accumulation: 64 products/lane × |x|·|t| stays under
    // 2^31 for any input the divergence bail (|taps|²<2500) permits.
    constexpr float X_SCALE = 1024.0f, T_SCALE = 2048.0f;
    constexpr float OUT_SCALE = 1.0f / (X_SCALE * T_SCALE);

    // Taps change only on field syncs (and DD/divergence paths); re-quantizing
    // 256 floats per 832-symbol segment is ~0.1% of the segment's MAC work —
    // cheaper than tracking dirtiness across every tap-writing code path.
    for (int k = 0; k < NTAPS; k++) {
        float v = d_taps[k] * T_SCALE;
        v = v > 32767.f ? 32767.f : (v < -32767.f ? -32767.f : v);
        d_tq[k] = (int16_t)lrintf(v);
    }
    const int span = nsamples + NTAPS;
    for (int k = 0; k < span; k++) {
        float v = input_samples[k] * X_SCALE;
        v = v > 32767.f ? 32767.f : (v < -32767.f ? -32767.f : v);
        d_xq[k] = (int16_t)lrintf(v);
    }
    memset(&d_xq[span], 0, 16 * sizeof(int16_t));

    int32_t o[4];
    int j = 0;
    for (; j + 4 <= nsamples; j += 4) {
        eq_fir_s16_x4(&d_xq[j], d_tq, NTAPS, o);
        output_samples[j + 0] = o[0] * OUT_SCALE;
        output_samples[j + 1] = o[1] * OUT_SCALE;
        output_samples[j + 2] = o[2] * OUT_SCALE;
        output_samples[j + 3] = o[3] * OUT_SCALE;
    }
    for (; j < nsamples; j++) {   // tail (never hit: 832 % 4 == 0)
        output_samples[j] = 0;
        volk_32f_x2_dot_prod_32f(
            &output_samples[j], &input_samples[j], &d_taps[0], NTAPS);
    }
}
#endif /* __ARM_NEON */

void atsc_equalizer_long_impl::filterN_dd(const float* input_samples,
                                          float* output_samples,
                                          int nsamples)
{
    // 2026-05-30 ROOT-CAUSE DRIFT FIX. The field-sync-only design freezes the
    // taps for 312 of every 313 segments; on a time-varying / noisy channel
    // the frozen taps go stale and, because each FS update only constrains the
    // ~728-symbol field-sync subspace (NTAPS can exceed that), the
    // unexcited tap directions random-walk under noise until the response is
    // garbage — the ~tens-of-seconds "clean then noise" drift we observe.
    //
    // The fix: track continuously between field syncs with confidence-gated
    // NLMS decision-directed adaptation. Three guards keep it from doing what
    // naive DD did before (diverge):
    //   1) GATE  — only adapt on confident decisions (|decision-y|<=gate); a
    //              closing eye stops feeding the loop wrong references, which
    //              is what causes the DD positive-feedback death spiral.
    //   2) NLMS  — step normalized by input power (μ/(ε+||x||²)); immune to
    //              the AGC/clipping power spikes that blow up fixed-step LMS.
    //   3) ANCHOR— the supervised FS-LMS still runs every field sync, pulling
    //              the taps back to ground truth so DD can only wander within
    //              one field (~24 ms), never permanently.
    // DD minimizes the SAME symbol-error objective as the FS-LMS anchor (unlike
    // CMA's constant-modulus objective, which fights the anchor and converged
    // to a wrong solution in testing — fs_mse≈90). Default OFF (μ=0).
    static const float DD_MU = []() -> float {
        if (const char* p = std::getenv("STVT_EQ_DD_MU")) {
            char* e = nullptr; double v = std::strtod(p, &e);
            if (e != p) return (float)v;
        }
        return 0.0f;
    }();
    static const float DD_GATE = []() -> float {
        if (const char* p = std::getenv("STVT_EQ_DD_GATE")) {
            char* e = nullptr; double v = std::strtod(p, &e);
            if (e != p) return (float)v;
        }
        return 1.0f;
    }();
    // BUGFIX 2026-05-30: default was 1e-4 applied PER DATA SEGMENT (~312×/field),
    // which collapsed the taps ~3%/field → output→0 → 0% clean even at μ=5e-7.
    // The field-sync LMS (adaptN) already leaks once per field on the SAME
    // d_taps; the data-segment path should not re-leak. Default 0 (no-op);
    // still tunable for experiments. If a gentle DD-path leak is ever wanted,
    // match the per-field rate: ~ (FS_LEAK / 312) ≈ 3e-6, NOT 1e-4.
    static const float DD_LEAK = []() -> float {
        if (const char* p = std::getenv("STVT_EQ_DD_LEAK")) {
            char* e = nullptr; double v = std::strtod(p, &e);
            if (e != p) return (float)v;
        }
        return 0.0f;
    }();
    static const float DD_EPS = 1.0f;  // NLMS regularizer, ~ noise floor power

    // μ==0 → behave exactly like the legacy passive filter (zero behavior
    // change when the knob is off).
    if (DD_MU <= 0.0f) {
        filterN(input_samples, output_samples, nsamples);
        return;
    }

    for (int j = 0; j < nsamples; j++) {
        const float* x = &input_samples[j];
        float y = 0.0f;
        volk_32f_x2_dot_prod_32f(&y, x, &d_taps[0], NTAPS);
        output_samples[j] = y;

        // 8-VSB slicer (levels ±1,±3,±5,±7 — same normalization as the FS
        // training, which sits at ±5).
        float decision;
        if      (y >=  6.0f) decision =  7.0f;
        else if (y >=  4.0f) decision =  5.0f;
        else if (y >=  2.0f) decision =  3.0f;
        else if (y >=  0.0f) decision =  1.0f;
        else if (y >= -2.0f) decision = -1.0f;
        else if (y >= -4.0f) decision = -3.0f;
        else if (y >= -6.0f) decision = -5.0f;
        else                 decision = -7.0f;

        float e = decision - y;             // target − output
        if (std::fabs(e) > DD_GATE) continue;   // unconfident — don't adapt

        float xnorm2 = 0.0f;
        volk_32f_x2_dot_prod_32f(&xnorm2, x, x, NTAPS);
        float mu_eff = DD_MU / (DD_EPS + xnorm2);
        float scale = mu_eff * e;
        if (!std::isfinite(scale)) continue;

        float tmp_taps[NTAPS];
        volk_32f_s32f_multiply_32f(tmp_taps, x, scale, NTAPS);
        volk_32f_x2_add_32f(&d_taps[0], &d_taps[0], tmp_taps, NTAPS);
    }

    // Optional leak (default 0 — see BUGFIX note above). When enabled, bounds
    // the unexcited-direction random walk between FS anchors.
    if (DD_LEAK > 0.0f) {
        float keep = 1.0f - DD_LEAK;
        for (int k = 0; k < NTAPS; k++) d_taps[k] *= keep;
    }

    // Cheap divergence backstop so a bad patch can't run away before the next
    // field sync. Mirror adaptN's policy: restore LKG if we have one, else
    // reset to a delta (pass-through).
    double tap_e = 0.0;
    for (int k = 0; k < NTAPS; k++) tap_e += (double)d_taps[k] * (double)d_taps[k];
    if (!std::isfinite(tap_e) || tap_e > 50.0 * 50.0) {
        if (d_lkg_valid) {
            for (int k = 0; k < NTAPS; k++) d_taps[k] = d_taps_lkg[k];
        } else {
            for (int k = 0; k < NTAPS; k++) d_taps[k] = 0.0f;
            d_taps[NPRETAPS] = 1.0f;
        }
        for (int j = 0; j < nsamples; j++)
            output_samples[j] = input_samples[j + NPRETAPS];
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
        return 1.5f;
    }();
    // FIX #3 (2026-05-23): coherent field-sync averaging depth.
    // 1 = no averaging (legacy). 2/4/8 = average N field syncs before LMS.
    // Signal=fixed (PN511/PN63 training), noise=iid → √N SNR gain on gradient.
    // Cost: adapt rate ÷ N. Tunable via STVT_EQ_FS_AVG_DEPTH.
    static const int FS_AVG_DEPTH = []() -> int {
        if (const char* p = std::getenv("STVT_EQ_FS_AVG_DEPTH")) {
            int v = std::atoi(p);
            if (v >= 1 && v <= 32) return v;
        }
        return 1;
    }();
    // 2026-05-26 PERIODIC LKG reset. 0 = disabled (default). Otherwise
    // force d_taps <- d_taps_lkg every N seconds, on top of the existing
    // divergence-triggered restore. Counters slow LMS drift to noise
    // that the divergence check doesn't catch in time.
    static const int RESET_INTERVAL_SEC = []() -> int {
        if (const char* p = std::getenv("STVT_EQ_RESET_INTERVAL")) {
            int v = std::atoi(p);
            if (v >= 0 && v <= 3600) return v;
        }
        return 0;
    }();
    // 2026-05-27 QUALITY-AWARE LKG reset. When err_rms (this batch) exceeds
    // QUALITY_BAD_RMS, force taps back to LKG snapshot — debounced so LMS
    // gets ~0.5s to reconverge before another reset fires. Replaces the
    // "wait N seconds and hope drift happens between resets" heuristic
    // with "actually react when the equalizer is producing garbage".
    // 0 = disabled (default — needs validation on a non-drought night).
    // Useful range: 5–10. <5 blocks initial convergence; >10 rarely fires.
    static const float QUALITY_BAD_RMS = []() -> float {
        if (const char* p = std::getenv("STVT_EQ_QUALITY_BAD_RMS")) {
            char* e = nullptr; double v = std::strtod(p, &e);
            if (e != p && v >= 0.0) return (float)v;
        }
        return 0.0f;
    }();
    static const int QUALITY_DEBOUNCE_MS = []() -> int {
        if (const char* p = std::getenv("STVT_EQ_QUALITY_DEBOUNCE_MS")) {
            int v = std::atoi(p);
            if (v >= 50 && v <= 10000) return v;
        }
        return 500;
    }();
    // 2026-05-28 GEAR-SHIFT LMS: separate step sizes for convergence vs
    // steady-state tracking. The math: LMS steady-state tap variance is
    // ≈ (μ/2)·σ²_e·N, so dropping μ by 50× drops drift by 50× — but
    // convergence time scales as 1/μ, so cold-starting at low μ never
    // locks. Solution: fast μ during convergence (err high), shift to
    // slow μ once err stays low for K segments, shift back on err spike.
    // Defaults disabled (FAST=SLOW=BETA) for backward compatibility.
    static const bool GEAR_ENABLED = []() -> bool {
        const char* p = std::getenv("STVT_EQ_GEAR_LMS");
        return p && std::atoi(p) != 0;
    }();
    static const double BETA_FAST = []() -> double {
        if (const char* p = std::getenv("STVT_EQ_BETA_FAST")) {
            char* e = nullptr; double v = std::strtod(p, &e);
            if (e != p) return v;
        }
        return 5e-5;   // matches default BETA — fast convergence
    }();
    static const double BETA_SLOW = []() -> double {
        if (const char* p = std::getenv("STVT_EQ_BETA_SLOW")) {
            char* e = nullptr; double v = std::strtod(p, &e);
            if (e != p) return v;
        }
        return 1e-6;   // ~50× less drift than 5e-5 in steady state
    }();
    static const float GEAR_LOW_ERR = []() -> float {
        if (const char* p = std::getenv("STVT_EQ_GEAR_LOW_ERR")) {
            char* e = nullptr; double v = std::strtod(p, &e);
            if (e != p) return (float)v;
        }
        return 1.0f;
    }();
    static const float GEAR_HIGH_ERR = []() -> float {
        if (const char* p = std::getenv("STVT_EQ_GEAR_HIGH_ERR")) {
            char* e = nullptr; double v = std::strtod(p, &e);
            if (e != p) return (float)v;
        }
        return 2.0f;
    }();
    static const int GEAR_DEBOUNCE = []() -> int {
        if (const char* p = std::getenv("STVT_EQ_GEAR_DEBOUNCE_BATCHES")) {
            int v = std::atoi(p);
            if (v >= 1 && v <= 100000) return v;
        }
        return 100;    // ~100 field syncs (~4 sec) of sustained low-err
    }();
    static bool _logged = []() {
        std::fprintf(stderr,
                     "[atsc_equalizer_long] tunable params: BETA=%g LEAK=%g DIVERGENCE_BAIL=%g LKG=%d LKG_RMS=%g FS_AVG_DEPTH=%d RESET_INTERVAL_SEC=%d GEAR=%d BETA_FAST=%g BETA_SLOW=%g GEAR_LOW=%g GEAR_HIGH=%g GEAR_DEBOUNCE=%d QUALITY_BAD_RMS=%g QUALITY_DEBOUNCE_MS=%d\n",
                     BETA, LEAK, DIVERGENCE_BAIL,
                     (int)LKG_ENABLED, LKG_GOOD_RMS_THRESHOLD, FS_AVG_DEPTH, RESET_INTERVAL_SEC,
                     (int)GEAR_ENABLED, BETA_FAST, BETA_SLOW, GEAR_LOW_ERR, GEAR_HIGH_ERR, GEAR_DEBOUNCE,
                     QUALITY_BAD_RMS, QUALITY_DEBOUNCE_MS);
        return true;
    }();

    // Coherent field-sync averaging path. Accumulate input_samples into
    // d_fs_acc; only run LMS update when FS_AVG_DEPTH syncs have been
    // accumulated. Output is always computed (downstream needs it).
    const float* lms_input = input_samples;
    const int input_span = nsamples + NTAPS;
    if (FS_AVG_DEPTH > 1 && input_span <= FS_ACC_LEN) {
        if (d_fs_count == 0) {
            std::memcpy(d_fs_acc, input_samples, input_span * sizeof(float));
        } else {
            for (int k = 0; k < input_span; k++) d_fs_acc[k] += input_samples[k];
        }
        d_fs_count++;
        if (d_fs_count < FS_AVG_DEPTH) {
            // Not enough accumulated yet — compute output from current taps,
            // apply LEAK only, then return (no LMS update this sync).
            for (int j = 0; j < nsamples; j++) {
                output_samples[j] = 0;
                volk_32f_x2_dot_prod_32f(
                    &output_samples[j], &input_samples[j], &d_taps[0], NTAPS);
            }
            float keep = 1.0f - LEAK;
            for (int k = 0; k < NTAPS; k++) d_taps[k] *= keep;
            return;
        }
        // Accumulator full: normalize to average, run LMS on it.
        const float scale = 1.0f / (float)FS_AVG_DEPTH;
        for (int k = 0; k < input_span; k++) d_fs_acc[k] *= scale;
        lms_input = d_fs_acc;
        d_fs_count = 0;
    }

    // Accumulate RMS error during this adapt batch for LKG quality assessment.
    double err_sq_sum = 0.0;

    // 2026-05-28 GEAR-SHIFT LMS state — track active μ and shift hysteresis.
    // When disabled, current_mu stays at BETA forever (legacy behavior).
    static double current_mu = BETA;
    static int    gear_low_err_count = 0;
    static int    gear_state = 0;   // 0=FAST, 1=SLOW
    const double effective_mu = GEAR_ENABLED ? current_mu : BETA;

    for (int j = 0; j < nsamples; j++) {
        output_samples[j] = 0;
        // Output uses current-field input (downstream sees this field).
        volk_32f_x2_dot_prod_32f(
            &output_samples[j], &input_samples[j], &d_taps[0], NTAPS);
        // LMS gradient uses lms_input (averaged when FS_AVG_DEPTH>1).
        float y_avg = output_samples[j];
        if (lms_input != input_samples) {
            y_avg = 0.0f;
            volk_32f_x2_dot_prod_32f(&y_avg, &lms_input[j], &d_taps[0], NTAPS);
        }
        float e = y_avg - training_pattern[j];
        err_sq_sum += (double)e * (double)e;
        float tmp_taps[NTAPS];
        volk_32f_s32f_multiply_32f(tmp_taps, &lms_input[j], effective_mu * e, NTAPS);
        volk_32f_x2_subtract_32f(&d_taps[0], &d_taps[0], tmp_taps, NTAPS);
    }

    float keep = 1.0f - LEAK;
    for (int k = 0; k < NTAPS; k++) d_taps[k] *= keep;

    // LKG snapshot: if THIS adapt batch had low error, this tap state is
    // probably good. Save it for future divergence recovery.
    const double batch_err_rms = (nsamples > 0)
        ? std::sqrt(err_sq_sum / (double)nsamples)
        : 0.0;
    if (LKG_ENABLED && nsamples > 0) {
        if (batch_err_rms < (double)LKG_GOOD_RMS_THRESHOLD) {
            for (int k = 0; k < NTAPS; k++) d_taps_lkg[k] = d_taps[k];
            d_lkg_valid = true;
        }
    }

    // 2026-05-30 DRIFT-LOCALIZATION TELEMETRY. Emit timestamped equalizer
    // quality every 8 field syncs (mirrors the CMA block's cadence) so it can
    // be correlated against [fpll t=...] to pin WHERE a drift starts — carrier
    // (FPLL nco/max|x|), equalizer (this fs_err_rms / |taps|), or downstream
    // (viterbi_metric). Gated by STVT_EQ_TELEM=1 (default off, zero overhead).
    static const bool TELEM = []() {
        const char* p = std::getenv("STVT_EQ_TELEM"); return p && std::atoi(p) != 0;
    }();
    if (TELEM && nsamples > 0) {
        static auto telem_t0 = std::chrono::steady_clock::now();
        static uint64_t telem_fs = 0;
        telem_fs++;
        if ((telem_fs % 8) == 0) {
            double tap_e = 0.0;
            for (int k = 0; k < NTAPS; k++) tap_e += (double)d_taps[k] * (double)d_taps[k];
            double t = std::chrono::duration<double>(
                           std::chrono::steady_clock::now() - telem_t0).count();
            std::fprintf(stderr,
                "[eq-long t=%6.2fs] fs=%llu fs_err_rms=%.4f |taps|=%.3f mu=%g\n",
                t, (unsigned long long)telem_fs, batch_err_rms,
                std::sqrt(tap_e), effective_mu);
        }
    }

    // 2026-05-28 GEAR-SHIFT LMS state update — after the LMS pass.
    // FAST→SLOW: sustained low err for GEAR_DEBOUNCE batches.
    // SLOW→FAST: any err spike above GEAR_HIGH_ERR (immediate, no debounce —
    //            we want fast convergence the moment quality degrades).
    if (GEAR_ENABLED && nsamples > 0) {
        if (batch_err_rms < (double)GEAR_LOW_ERR) {
            gear_low_err_count++;
            if (gear_state == 0 && gear_low_err_count >= GEAR_DEBOUNCE) {
                current_mu = BETA_SLOW;
                gear_state = 1;
                static uint64_t shift_downs = 0;
                shift_downs++;
                if (shift_downs <= 5 || (shift_downs & 0x3F) == 0) {
                    std::fprintf(stderr,
                                 "[atsc_equalizer_long] GEAR shift→SLOW #%llu (err_rms=%g, μ %g→%g)\n",
                                 (unsigned long long)shift_downs,
                                 batch_err_rms, BETA_FAST, BETA_SLOW);
                }
            }
        } else {
            gear_low_err_count = 0;
            if (gear_state == 1 && batch_err_rms > (double)GEAR_HIGH_ERR) {
                current_mu = BETA_FAST;
                gear_state = 0;
                static uint64_t shift_ups = 0;
                shift_ups++;
                if (shift_ups <= 5 || (shift_ups & 0x3F) == 0) {
                    std::fprintf(stderr,
                                 "[atsc_equalizer_long] GEAR shift→FAST #%llu (err_rms=%g, μ %g→%g)\n",
                                 (unsigned long long)shift_ups,
                                 batch_err_rms, BETA_SLOW, BETA_FAST);
                }
            }
        }
    }

    // PERIODIC LKG reset: force taps back to known-good snapshot every N sec.
    // Counters slow LMS drift to noise that doesn't trigger DIVERGENCE_BAIL.
    if (RESET_INTERVAL_SEC > 0 && LKG_ENABLED && d_lkg_valid) {
        static auto d_last_periodic_reset = std::chrono::steady_clock::now();
        auto _now = std::chrono::steady_clock::now();
        auto _elapsed = std::chrono::duration_cast<std::chrono::seconds>(
                            _now - d_last_periodic_reset).count();
        if (_elapsed >= RESET_INTERVAL_SEC) {
            for (int k = 0; k < NTAPS; k++) d_taps[k] = d_taps_lkg[k];
            d_last_periodic_reset = _now;
            static uint64_t periodic_resets = 0;
            periodic_resets++;
            if (periodic_resets <= 5 || (periodic_resets & 0x3F) == 0) {
                std::fprintf(stderr,
                             "[atsc_equalizer_long] periodic LKG reset #%llu (every %ds)\n",
                             (unsigned long long)periodic_resets, RESET_INTERVAL_SEC);
            }
        }
    }

    // 2026-05-27 QUALITY-AWARE LKG reset: react to actual error, not a clock.
    // err_rms from this LMS batch is the equalizer's own quality signal.
    // When it spikes above QUALITY_BAD_RMS (and we have a saved snapshot),
    // taps have drifted badly — restore. Debounced so LMS gets time to
    // reconverge before we slam it again.
    if (QUALITY_BAD_RMS > 0.0f && LKG_ENABLED && d_lkg_valid && nsamples > 0) {
        const double err_rms_now = std::sqrt(err_sq_sum / (double)nsamples);
        static auto d_last_quality_reset = std::chrono::steady_clock::now()
                                           - std::chrono::seconds(10);
        if (err_rms_now > (double)QUALITY_BAD_RMS) {
            auto _now = std::chrono::steady_clock::now();
            auto _ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                            _now - d_last_quality_reset).count();
            if (_ms >= QUALITY_DEBOUNCE_MS) {
                for (int k = 0; k < NTAPS; k++) d_taps[k] = d_taps_lkg[k];
                d_last_quality_reset = _now;
                static uint64_t quality_resets = 0;
                quality_resets++;
                if (quality_resets <= 5 || (quality_resets & 0x3F) == 0) {
                    std::fprintf(stderr,
                                 "[atsc_equalizer_long] QUALITY LKG reset #%llu (err_rms=%g > %g)\n",
                                 (unsigned long long)quality_resets,
                                 err_rms_now, (double)QUALITY_BAD_RMS);
                }
            }
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

void atsc_equalizer_long_impl::adaptN_rls(const float* input_samples,
                                          const float* training_pattern,
                                          float* output_samples,
                                          int nsamples)
{
    // Recursive Least Squares on the field-sync training sequence. RLS tracks
    // a time-varying channel far better than LMS (the drift cause), converging
    // in ~N samples instead of LMS's many fields. Forgetting factor LAMBDA<1
    // weights recent data — smaller = faster tracking, less stable. P is the
    // (inverse autocorrelation) matrix, init P = (1/DELTA)·I. Standard RLS:
    //   pi   = P x
    //   k    = pi / (LAMBDA + xᵀ pi)
    //   xi   = d - wᵀx            (a-priori error)
    //   w   += k · xi
    //   P    = (P - k piᵀ) / LAMBDA
    // Double precision throughout for stability; taps cast to float for volk.
    static const double LAMBDA = []() -> double {
        if (const char* p = std::getenv("STVT_EQ_RLS_LAMBDA")) {
            char* e=nullptr; double v=std::strtod(p,&e); if(e!=p) return v; }
        return 0.9995;
    }();
    static const double DELTA = []() -> double {
        if (const char* p = std::getenv("STVT_EQ_RLS_DELTA")) {
            char* e=nullptr; double v=std::strtod(p,&e); if(e!=p) return v; }
        return 0.01;
    }();
    static const bool TELEM = []() {
        const char* p = std::getenv("STVT_EQ_TELEM"); return p && std::atoi(p)!=0;
    }();
    static const bool LKG_ENABLED = []() {
        const char* p = std::getenv("STVT_EQ_LKG"); return p && std::atoi(p)!=0;
    }();

    if (!d_rls_inited) {
        d_rls_P.assign((size_t)NTAPS * NTAPS, 0.0);
        for (int i = 0; i < NTAPS; i++) d_rls_P[(size_t)i*NTAPS + i] = 1.0/DELTA;
        d_rls_inited = true;
    }
    double* P = d_rls_P.data();
    const double invLam = 1.0 / LAMBDA;
    std::vector<double> pi(NTAPS), kk(NTAPS);
    double err_sq_sum = 0.0;

    for (int j = 0; j < nsamples; j++) {
        const float* x = &input_samples[j];
        float yf = 0.0f;
        volk_32f_x2_dot_prod_32f(&yf, x, &d_taps[0], NTAPS);
        output_samples[j] = yf;

        // pi = P x   (O(N^2))
        for (int r = 0; r < NTAPS; r++) {
            const double* Pr = &P[(size_t)r*NTAPS];
            double s = 0.0;
            for (int c = 0; c < NTAPS; c++) s += Pr[c] * (double)x[c];
            pi[r] = s;
        }
        double xpi = 0.0;
        for (int c = 0; c < NTAPS; c++) xpi += (double)x[c] * pi[c];
        double denom = LAMBDA + xpi;
        if (denom < 1e-12) denom = 1e-12;
        for (int r = 0; r < NTAPS; r++) kk[r] = pi[r] / denom;

        double xi = (double)training_pattern[j] - (double)yf;
        err_sq_sum += xi * xi;
        for (int r = 0; r < NTAPS; r++) d_taps[r] += (float)(kk[r] * xi);

        // P = (P - k piᵀ)/LAMBDA   (O(N^2))
        for (int r = 0; r < NTAPS; r++) {
            double kr = kk[r];
            double* Pr = &P[(size_t)r*NTAPS];
            for (int c = 0; c < NTAPS; c++) Pr[c] = (Pr[c] - kr*pi[c]) * invLam;
        }
    }

    const double batch_err_rms = (nsamples > 0)
        ? std::sqrt(err_sq_sum / (double)nsamples) : 0.0;
    if (LKG_ENABLED && nsamples > 0 && batch_err_rms < 1.5) {
        for (int k = 0; k < NTAPS; k++) d_taps_lkg[k] = d_taps[k];
        d_lkg_valid = true;
    }

    // Divergence backstop: if taps blow up or go non-finite, reset taps AND
    // reinitialize P (a diverged P keeps re-diverging otherwise).
    double tap_e = 0.0;
    for (int k = 0; k < NTAPS; k++) tap_e += (double)d_taps[k]*(double)d_taps[k];
    if (!std::isfinite(tap_e) || tap_e > 2500.0) {
        if (LKG_ENABLED && d_lkg_valid) {
            for (int k = 0; k < NTAPS; k++) d_taps[k] = d_taps_lkg[k];
        } else {
            for (int k = 0; k < NTAPS; k++) d_taps[k] = 0.0f;
            d_taps[NPRETAPS] = 1.0f;
        }
        std::fill(d_rls_P.begin(), d_rls_P.end(), 0.0);
        for (int i = 0; i < NTAPS; i++) d_rls_P[(size_t)i*NTAPS + i] = 1.0/DELTA;
        for (int j = 0; j < nsamples; j++)
            output_samples[j] = input_samples[j + NPRETAPS];
    }

    if (TELEM && nsamples > 0) {
        static auto rls_t0 = std::chrono::steady_clock::now();
        static uint64_t rls_fs = 0;
        rls_fs++;
        if ((rls_fs % 8) == 0) {
            double t = std::chrono::duration<double>(
                           std::chrono::steady_clock::now() - rls_t0).count();
            std::fprintf(stderr,
                "[eq-rls t=%6.2fs] fs=%llu fs_err_rms=%.4f |taps|=%.3f lambda=%g\n",
                t, (unsigned long long)rls_fs, batch_err_rms,
                std::sqrt(tap_e), LAMBDA);
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
            // RLS field-sync adaptation when STVT_EQ_RLS=1, else LMS (default).
            static const bool RLS_ENABLED = []() {
                const char* p = std::getenv("STVT_EQ_RLS"); return p && std::atoi(p) != 0;
            }();
            const float* trn = (d_flags & 0x0010) ? training_sequence2 : training_sequence1;
            if (RLS_ENABLED) {
                adaptN_rls(data_mem, trn, data_mem2, KNOWN_FIELD_SYNC_LENGTH);
            } else if (d_flags & 0x0010) {
                adaptN(data_mem, training_sequence2, data_mem2, KNOWN_FIELD_SYNC_LENGTH);
            } else {
                adaptN(data_mem, training_sequence1, data_mem2, KNOWN_FIELD_SYNC_LENGTH);
            }
        } else {
            // Continuous decision-directed tracking on data segments (no-op
            // passive filter when STVT_EQ_DD_MU is unset/0).
            filterN_dd(data_mem, data_mem2, ATSC_DATA_SEGMENT_LENGTH);

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
