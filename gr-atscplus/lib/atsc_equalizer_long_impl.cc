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
#include <algorithm>
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

    d_fb.assign(NFB_MAX, 0.0f);        // DFE feedback taps (start silent)
    d_hist.assign(2 * NFB_MAX, 0.0f);  // decided-symbol ring, double-length

    // 2026-07-05 WARM START (research lever #3): seed the taps from the
    // previous session's vetted LKG snapshot for this channel+antenna.
    // The cache holds only quality-gated tap states, so the worst case is
    // "slightly stale but sane" — LMS adapts from there far faster than
    // from a delta. STVT_EQ_TAP_CACHE_FILE is composed by tv_live.py
    // (dir + antenna + rf); absent = exactly the legacy cold start.
    if (const char* p = std::getenv("STVT_EQ_TAP_CACHE_FILE")) {
        std::FILE* f = std::fopen(p, "rb");
        if (f) {
            uint32_t magic = 0, n = 0;
            if (std::fread(&magic, 4, 1, f) == 1 &&
                std::fread(&n, 4, 1, f) == 1 &&
                magic == 0x54415043u /* 'TAPC' */ && n == (uint32_t)NTAPS) {
                std::vector<float> t(NTAPS);
                if (std::fread(t.data(), sizeof(float), NTAPS, f)
                        == (size_t)NTAPS) {
                    double e = 0.0;
                    bool fin = true;
                    for (int k = 0; k < NTAPS; k++) {
                        if (!std::isfinite(t[k])) { fin = false; break; }
                        e += (double)t[k] * (double)t[k];
                    }
                    if (fin && e > 0.01 && e < 50.0 * 50.0) {
                        d_taps = t;
                        d_taps_lkg = t;
                        d_lkg_valid = true;
                        std::fprintf(stderr,
                            "[eq-long] WARM START from %s (|taps|=%.3f)\n",
                            p, std::sqrt(e));
                    }
                }
            }
            std::fclose(f);
        }
    }

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

void atsc_equalizer_long_impl::dfe_push(float sym, int nfb)
{
    // double-length ring: writing each symbol at h and h+nfb keeps the
    // most recent nfb decisions contiguous at [h+1 .. h+nfb]
    d_hpos = (d_hpos + 1) % nfb;
    d_hist[d_hpos] = sym;
    d_hist[d_hpos + nfb] = sym;
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

    // ── DFE v1 (2026-07-06, docs/DFE_BLUEPRINT.md) ──
    // STVT_EQ_DFE=1 enables the feedback section: already-decided symbols
    // are SUBTRACTED (echo cancellation without noise enhancement — the
    // linear FFE must invert echoes, a DFE just removes them). Data
    // segments only in v1; general_work refills the decision history with
    // KNOWN training symbols each field sync (error-propagation flush).
    static const bool DFE = []() {
        const char* p = std::getenv("STVT_EQ_DFE");
        return p && std::atoi(p) != 0;
    }();
    static const int NFB = []() -> int {
        if (const char* p = std::getenv("STVT_EQ_DFE_NFB")) {
            int v = std::atoi(p);
            if (v >= 16 && v <= NFB_MAX) return v;
        }
        return 192;
    }();
    static const float MU_FB = []() -> float {
        if (const char* p = std::getenv("STVT_EQ_DFE_MU_FB")) {
            char* e = nullptr; double v = std::strtod(p, &e);
            if (e != p) return (float)v;
        }
        return 2e-3f;
    }();
    // decided 8-VSB symbols have fixed power E[d^2]=21 → constant NLMS
    // normalizer for the feedback update (no per-sample dot needed)
    const float mu_fb_eff = MU_FB / (1.0f + 21.0f * (float)NFB);

    // μ==0 → behave exactly like the legacy passive filter (zero behavior
    // change when the knob is off). Sheriff suspension counts as off.
    const bool dfe_on = DFE && !d_dfe_suspend;
    if (DD_MU <= 0.0f && !dfe_on) {
        filterN(input_samples, output_samples, nsamples);
        return;
    }

    for (int j = 0; j < nsamples; j++) {
        const float* x = &input_samples[j];
        float y = 0.0f;
        volk_32f_x2_dot_prod_32f(&y, x, &d_taps[0], NTAPS);
        const float* hist_w = &d_hist[d_hpos + 1];   // last NFB decisions
        if (dfe_on) {
            float fb = 0.0f;
            volk_32f_x2_dot_prod_32f(&fb, hist_w, &d_fb[0], NFB);
            y -= fb;
        }
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
        const bool confident = std::fabs(e) <= DD_GATE;
        if (dfe_on && confident && std::isfinite(e)) {
            // feedback update BEFORE the push — the push mutates the
            // window's oldest slot (v1 bug: update-after-push fed the
            // newest decision into the oldest tap's gradient)
            float tmp_fb[NFB_MAX];
            volk_32f_s32f_multiply_32f(tmp_fb, hist_w, mu_fb_eff * e, NFB);
            volk_32f_x2_subtract_32f(&d_fb[0], &d_fb[0], tmp_fb, NFB);
        }
        if (dfe_on)
            dfe_push(decision, NFB);        // history advances every symbol
        if (!confident) continue;           // unconfident — don't adapt FFE

        if (DD_MU <= 0.0f) continue;        // DFE-only mode: FFE stays FS-anchored

        float xnorm2 = 0.0f;
        volk_32f_x2_dot_prod_32f(&xnorm2, x, x, NTAPS);
        float mu_eff = DD_MU / (DD_EPS + xnorm2);
        float scale = mu_eff * e;
        if (!std::isfinite(scale)) continue;

        float tmp_taps[NTAPS];
        volk_32f_s32f_multiply_32f(tmp_taps, x, scale, NTAPS);
        volk_32f_x2_add_32f(&d_taps[0], &d_taps[0], tmp_taps, NTAPS);
    }

    // DFE per-field-ish leak: bounds FFE/FBF disagreement growth (v1 runs
    // the FS anchor WITHOUT feedback awareness — see blueprint; the leak
    // plus the confidence gate keep any double-cancellation self-limiting)
    if (dfe_on) {
        for (int k = 0; k < NFB; k++) d_fb[k] *= 0.9995f;
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

    // 2026-07-05 IMPULSE-GATED ADAPTATION FREEZE (stop-and-go LMS, research
    // hypothesis H1). Adaptation only happens on field syncs; when an
    // impulse burst lands ON the training sequence, the LMS eats a garbage
    // gradient at full step and the taps take several fields to recover —
    // and in cliff mode the same corrupt batch can fire a spurious QUALITY
    // reset. Guard: compute the batch's a-priori error BEFORE updating; if
    // it exceeds IMPULSE_K × the running EWMA of accepted batches, freeze —
    // no tap update, no leak, no LKG/gear/quality decisions from poisoned
    // data. Output is still produced (downstream needs the field sync).
    // Warmup: never freeze during initial convergence (errors are honestly
    // large there). Default OFF for A/B integrity (STVT_EQ_IMPULSE_GUARD=1).
    static const bool IMPULSE_GUARD = []() -> bool {
        const char* p = std::getenv("STVT_EQ_IMPULSE_GUARD");
        return p && std::atoi(p) != 0;
    }();
    static const float IMPULSE_K = []() -> float {
        if (const char* p = std::getenv("STVT_EQ_IMPULSE_K")) {
            char* e = nullptr; double v = std::strtod(p, &e);
            if (e != p && v > 1.0) return (float)v;
        }
        return 2.5f;
    }();
    static const int IMPULSE_WARMUP = []() -> int {
        if (const char* p = std::getenv("STVT_EQ_IMPULSE_WARMUP")) {
            int v = std::atoi(p);
            if (v >= 1 && v <= 10000) return v;
        }
        return 40;   // ~1 s of field syncs before the guard may engage
    }();
    static double imp_ewma = -1.0;
    static int    imp_warm = 0;
    static uint64_t imp_freezes = 0;

    if (IMPULSE_GUARD && nsamples > 0) {
        double pre_sq = 0.0;
        for (int j = 0; j < nsamples; j++) {
            float y_pre = 0.0f;
            volk_32f_x2_dot_prod_32f(&y_pre, &lms_input[j], &d_taps[0], NTAPS);
            const float e_pre = y_pre - training_pattern[j];
            pre_sq += (double)e_pre * (double)e_pre;
        }
        const double pre_rms = std::sqrt(pre_sq / (double)nsamples);
        const bool frozen = (imp_warm >= IMPULSE_WARMUP && imp_ewma > 0.0
                             && pre_rms > (double)IMPULSE_K * imp_ewma);
        if (frozen) {
            imp_freezes++;
            if (imp_freezes <= 5 || (imp_freezes & 0x3F) == 0) {
                std::fprintf(stderr,
                             "[atsc_equalizer_long] IMPULSE FREEZE #%llu "
                             "(batch err %.3f > %.1fx ewma %.3f) — taps held\n",
                             (unsigned long long)imp_freezes, pre_rms,
                             (double)IMPULSE_K, imp_ewma);
            }
            // produce output from the untouched taps, change nothing else
            for (int j = 0; j < nsamples; j++) {
                output_samples[j] = 0;
                volk_32f_x2_dot_prod_32f(
                    &output_samples[j], &input_samples[j], &d_taps[0], NTAPS);
            }
            return;
        }
        imp_ewma = (imp_ewma < 0.0) ? pre_rms
                                    : 0.95 * imp_ewma + 0.05 * pre_rms;
        imp_warm++;
    }

    // 2026-07-05 ROBUST (HUBER) CONFIDENCE-WEIGHTED LMS — hypothesis H1
    // step 3, the knobless successor to the binary impulse freeze. Every
    // training sample's gradient contribution is weighted by how trustworthy
    // its error looks: full strength up to HUBER_K × the running robust
    // scale (EWMA of the batch MEDIAN |e| — medians shrug off impulses),
    // clamped beyond it. An impulse corrupting 30 of 832 training symbols
    // costs those 30 samples their influence — the other 802 still teach at
    // full speed, unlike the batch freeze which discards everything.
    // HUBER_K is dimensionless (≈ sigma units, default 3.0) so it transfers
    // across antennas/channels — the scale itself is what self-calibrates.
    // Default OFF (STVT_EQ_ROBUST=1 to enable) pending live A/B.
    static const bool ROBUST = []() -> bool {
        const char* p = std::getenv("STVT_EQ_ROBUST");
        return p && std::atoi(p) != 0;
    }();
    static const float HUBER_K = []() -> float {
        if (const char* p = std::getenv("STVT_EQ_HUBER_K")) {
            char* e = nullptr; double v = std::strtod(p, &e);
            if (e != p && v > 0.5) return (float)v;
        }
        return 3.0f;
    }();
    static double rob_scale = -1.0;   // EWMA of batch median |e|
    static uint64_t rob_clamped = 0;  // samples clamped, lifetime
    float abs_e_buf[1024];
    const bool rob_active = ROBUST && nsamples > 0 && nsamples <= 1024;

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
        err_sq_sum += (double)e * (double)e;   // raw error: telemetry/quality
        float e_adapt = e;                     // gradient error: maybe clamped
        if (rob_active) {
            abs_e_buf[j] = std::fabs(e);
            if (rob_scale > 0.0) {
                const float c = HUBER_K * (float)rob_scale;
                if (e_adapt >  c) { e_adapt =  c; rob_clamped++; }
                if (e_adapt < -c) { e_adapt = -c; rob_clamped++; }
            }
        }
        float tmp_taps[NTAPS];
        volk_32f_s32f_multiply_32f(tmp_taps, &lms_input[j], effective_mu * e_adapt, NTAPS);
        volk_32f_x2_subtract_32f(&d_taps[0], &d_taps[0], tmp_taps, NTAPS);
    }

    // Robust scale update: batch median of |e| via nth_element, EWMA'd.
    // The median is what makes this impulse-proof — a burst corrupting a
    // minority of samples barely moves it, so the clamp level stays honest.
    if (rob_active) {
        float* mid = abs_e_buf + nsamples / 2;
        std::nth_element(abs_e_buf, mid, abs_e_buf + nsamples);
        const double med = (double)*mid;
        rob_scale = (rob_scale < 0.0) ? med : 0.95 * rob_scale + 0.05 * med;
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

    // WARM-START periodic persist: our chains die by force-kill, so the
    // cache must be written DURING life, not at exit. Every ~1024 field
    // syncs (~25 s) with a valid LKG, write it atomically (tmp+rename).
    static const char* TAP_CACHE_FILE = std::getenv("STVT_EQ_TAP_CACHE_FILE");
    if (TAP_CACHE_FILE && d_lkg_valid && nsamples > 0) {
        static uint64_t save_fs = 0;
        save_fs++;
        if ((save_fs & 0x3FF) == 0) {
            std::string tmp = std::string(TAP_CACHE_FILE) + ".tmp";
            std::FILE* f = std::fopen(tmp.c_str(), "wb");
            if (f) {
                const uint32_t magic = 0x54415043u, n = (uint32_t)NTAPS;
                std::fwrite(&magic, 4, 1, f);
                std::fwrite(&n, 4, 1, f);
                std::fwrite(d_taps_lkg.data(), sizeof(float), NTAPS, f);
                std::fclose(f);
                std::remove(TAP_CACHE_FILE);
                std::rename(tmp.c_str(), TAP_CACHE_FILE);
            }
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
                "[eq-long t=%6.2fs] fs=%llu fs_err_rms=%.4f |taps|=%.3f mu=%g "
                "frz=%llu hub=%.3f clmp=%llu\n",
                t, (unsigned long long)telem_fs, batch_err_rms,
                std::sqrt(tap_e), effective_mu,
                (unsigned long long)imp_freezes,
                rob_scale > 0.0 ? rob_scale : 0.0,
                (unsigned long long)rob_clamped);
        }
    }

    // 2026-07-05 CIR TELEMETRY — the "echo X-ray" (research hypothesis H2).
    // Correlating the received field sync against the known PN511 training
    // sequence yields the channel impulse response directly: every echo's
    // delay and strength, ~5×/s. Powers the panel's echo viewer (aim by
    // killing reflections instead of chasing a scalar MER) and, later,
    // analytic tap seeding. One symbol ≈ 92.9 ns ≈ 28 m of path difference.
    // STVT_EQ_CIR=1 to enable; ~230k MACs per emission — negligible.
    static const bool CIR_ENABLED = []() {
        const char* p = std::getenv("STVT_EQ_CIR");
        return p && std::atoi(p) != 0;
    }();
    // Coherent averaging (2026-07-05, v2): the field sync is symbol-locked,
    // so real echoes correlate with the SAME sign every sync while noise and
    // data-crosstalk flip randomly — summing signed correlations over
    // CIR_AVG syncs sinks the noise floor by √N (16 syncs ≈ −12 dB), which
    // is what lets the −34 dB threshold below stay honest.
    if (CIR_ENABLED && nsamples == KNOWN_FIELD_SYNC_LENGTH) {
        static const int CIR_AVG = []() -> int {
            if (const char* p = std::getenv("STVT_EQ_CIR_AVG")) {
                int v = std::atoi(p);
                if (v >= 1 && v <= 128) return v;
            }
            return 16;
        }();
        const float* pn = training_pattern + 4;      // the PN511 span
        const int NDELAY = nsamples + NTAPS - 511;
        static std::vector<double> cir_acc;
        static int cir_cnt = 0;
        if ((int)cir_acc.size() != NDELAY) {
            cir_acc.assign(NDELAY, 0.0);
            cir_cnt = 0;
        }
        for (int d = 0; d < NDELAY; d++) {
            float r = 0.0f;
            volk_32f_x2_dot_prod_32f(&r, &input_samples[d], pn, 511);
            cir_acc[d] += (double)r;                 // signed — coherent
        }
        cir_cnt++;
        if (cir_cnt >= CIR_AVG) {
            double peak = 0.0;
            int dpeak = 0;
            for (int d = 0; d < NDELAY; d++) {
                const double m = std::fabs(cir_acc[d]);
                if (m > peak) { peak = m; dpeak = d; }
            }
            if (peak > 0.0) {
                static auto cir_t0 = std::chrono::steady_clock::now();
                double t = std::chrono::duration<double>(
                               std::chrono::steady_clock::now() - cir_t0).count();
                char line[1600];
                int len = std::snprintf(line, sizeof(line), "[cir t=%.2f] ", t);
                int emitted = 0;
                // averaged floor ≈ −27 dB − 20·log10(√CIR_AVG); with the
                // default 16 that's ≈ −39 dB, so −34 dB bins are real.
                const double thresh = 0.02 * peak;
                for (int d = 0; d < NDELAY && emitted < 32; d++) {
                    const double m = std::fabs(cir_acc[d]);
                    if (m < thresh) continue;
                    if (d != dpeak && std::abs(d - dpeak) <= 2) continue;
                    const double db = 20.0 * std::log10(m / peak);
                    len += std::snprintf(line + len, sizeof(line) - len,
                                         "%d:%.1f,", d - dpeak, db);
                    emitted++;
                    if (len > (int)sizeof(line) - 32) break;
                }
                std::fprintf(stderr, "%s\n", line);
            }
            // 2026-07-06 Wiener warm-start: dump the SIGNED averaged CIR
            // (the stderr line above is magnitude-only peaks; the analytic
            // tap solve needs signs). STVT_EQ_CIR_DUMP=<path>, overwritten
            // each emission — latest channel snapshot wins.
            static const char* CIR_DUMP = std::getenv("STVT_EQ_CIR_DUMP");
            if (CIR_DUMP) {
                FILE* fd = std::fopen(CIR_DUMP, "wb");
                if (fd) {
                    const uint32_t magic = 0x43495244u; /* 'CIRD' */
                    const uint32_t n = (uint32_t)NDELAY;
                    std::fwrite(&magic, 4, 1, fd);
                    std::fwrite(&n, 4, 1, fd);
                    std::vector<float> tmpf(NDELAY);
                    for (int d = 0; d < NDELAY; d++)
                        tmpf[d] = (float)cir_acc[d];
                    std::fwrite(tmpf.data(), 4, NDELAY, fd);
                    std::fclose(fd);
                }
            }
            std::fill(cir_acc.begin(), cir_acc.end(), 0.0);
            cir_cnt = 0;
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
                // 2026-07-07 RESEED-ON-COLLAPSE (STVT_EQ_RESEED=1): re-read
                // the tap-cache FILE instead of the in-RAM snapshot. The
                // file can be refreshed mid-run by outside intelligence
                // (Wiener solve of a fresh CIR, a better session's LKG) —
                // recovery becomes a jump to current knowledge, not a
                // crawl back from a stale snapshot.
                static const bool RESEED = []() {
                    const char* p = std::getenv("STVT_EQ_RESEED");
                    return p && std::atoi(p) != 0;
                }();
                bool reseeded = false;
                if (RESEED) {
                    if (const char* cf =
                            std::getenv("STVT_EQ_TAP_CACHE_FILE")) {
                        std::FILE* f = std::fopen(cf, "rb");
                        if (f) {
                            uint32_t mg = 0, n = 0;
                            std::vector<float> t(NTAPS);
                            if (std::fread(&mg, 4, 1, f) == 1 &&
                                std::fread(&n, 4, 1, f) == 1 &&
                                mg == 0x54415043u && n == (uint32_t)NTAPS &&
                                std::fread(t.data(), 4, NTAPS, f)
                                    == (size_t)NTAPS) {
                                double e2 = 0.0;
                                bool fin = true;
                                for (int k = 0; k < NTAPS; k++) {
                                    if (!std::isfinite(t[k])) {
                                        fin = false;
                                        break;
                                    }
                                    e2 += (double)t[k] * (double)t[k];
                                }
                                if (fin && e2 > 0.01 && e2 < 2500.0) {
                                    for (int k = 0; k < NTAPS; k++)
                                        d_taps[k] = t[k];
                                    reseeded = true;
                                }
                            }
                            std::fclose(f);
                        }
                    }
                }
                if (!reseeded)
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
            // ── E5 COMMAND PORT (2026-07-06) ── the FEC sheriff's lever.
            // RS truth lives OUTSIDE this block's influence; when an
            // external supervisor sees the confidently-wrong signature
            // (healthy fs_err + massive RS failure), it drops a one-word
            // command file that we poll once per field sync (~21 Hz,
            // one stat() — negligible). Commands: lkg | delta | cache |
            // dfe0 | dfe1. File is consumed (deleted) on execution.
            static const char* CMD_FILE = std::getenv("STVT_EQ_CMD_FILE");
            if (CMD_FILE) {
                std::FILE* cf = std::fopen(CMD_FILE, "rb");
                if (cf) {
                    char cmd[16] = { 0 };
                    size_t n = std::fread(cmd, 1, sizeof(cmd) - 1, cf);
                    std::fclose(cf);
                    std::remove(CMD_FILE);
                    while (n && (cmd[n - 1] == '\n' || cmd[n - 1] == '\r' ||
                                 cmd[n - 1] == ' '))
                        cmd[--n] = 0;
                    bool ok = true;
                    if (!std::strcmp(cmd, "lkg") && d_lkg_valid) {
                        d_taps = d_taps_lkg;
                        std::fill(d_fb.begin(), d_fb.end(), 0.0f);
                    } else if (!std::strcmp(cmd, "delta")) {
                        std::fill(d_taps.begin(), d_taps.end(), 0.0f);
                        d_taps[NPRETAPS] = 1.0f;
                        std::fill(d_fb.begin(), d_fb.end(), 0.0f);
                    } else if (!std::strcmp(cmd, "dfe0")) {
                        std::fill(d_fb.begin(), d_fb.end(), 0.0f);
                        d_dfe_suspend = true;
                    } else if (!std::strcmp(cmd, "dfe1")) {
                        d_dfe_suspend = false;
                    } else if (!std::strcmp(cmd, "cache")) {
                        if (const char* p =
                                std::getenv("STVT_EQ_TAP_CACHE_FILE")) {
                            std::FILE* f = std::fopen(p, "rb");
                            if (f) {
                                uint32_t mg = 0, nn = 0;
                                std::vector<float> t(NTAPS);
                                if (std::fread(&mg, 4, 1, f) == 1 &&
                                    std::fread(&nn, 4, 1, f) == 1 &&
                                    mg == 0x54415043u &&
                                    nn == (uint32_t)NTAPS &&
                                    std::fread(t.data(), 4, NTAPS, f)
                                        == (size_t)NTAPS)
                                    d_taps = t;
                                std::fclose(f);
                            }
                        }
                        std::fill(d_fb.begin(), d_fb.end(), 0.0f);
                    } else {
                        ok = false;
                    }
                    std::fprintf(stderr, "[eq-long] SHERIFF cmd '%s'%s\n",
                                 cmd, ok ? "" : " (unknown)");
                }
            }
            // MOD-12 GUARD detection: at every field sync the emitted
            // data-segment count must be ≡ 0 (mod 12). If not, a stream
            // discontinuity rotated the viterbi grid — schedule a drop
            // of the deficit so the next segments realign it.
            {
                static const bool MOD12_G = []() {
                    // default ON since 2026-07-07 (gauntlet-passed)
                    const char* p = std::getenv("STVT_EQ_MOD12_GUARD");
                    return !(p && p[0] == '0');
                }();
                if (MOD12_G && d_mod12_drop == 0) {
                    // v2 DEBOUNCE (2026-07-07 gauntlet lesson): a single
                    // glitched FS detection reads a bogus nonzero phase;
                    // realigning against it DROPS GOOD SEGMENTS (cliff
                    // cell ran worse with v1 in storm conditions). Real
                    // slips persist — same phase, field after field
                    // (456/456 last night). Fire only on a repeat.
                    if (d_mod12_count != 0 &&
                        d_mod12_count == d_mod12_pending) {
                        // drop k=c: next field start lands at
                        // (c + 312 - c) ≡ 0 (mod 12). Counter is NOT
                        // reset — absolute phase tracking, else drops
                        // oscillate forever.
                        d_mod12_drop = d_mod12_count;
                        d_mod12_pending = -1;
                        std::fprintf(stderr,
                                     "[eq-long] MOD12 GUARD: phase %d "
                                     "confirmed x2 — dropping %d segments\n",
                                     d_mod12_count, d_mod12_drop);
                    } else {
                        d_mod12_pending =
                            (d_mod12_count != 0) ? d_mod12_count : -1;
                    }
                }
            }
            // RLS field-sync adaptation when STVT_EQ_RLS=1, else LMS (default).
            static const bool RLS_ENABLED = []() {
                const char* p = std::getenv("STVT_EQ_RLS"); return p && std::atoi(p) != 0;
            }();
            const float* trn = (d_flags & 0x0010) ? training_sequence2 : training_sequence1;
            static const bool DFE_G = []() {
                const char* p = std::getenv("STVT_EQ_DFE");
                return p && std::atoi(p) != 0;
            }();
            static const int NFB_G = []() -> int {
                if (const char* p = std::getenv("STVT_EQ_DFE_NFB")) {
                    int v = std::atoi(p);
                    if (v >= 16 && v <= NFB_MAX) return v;
                }
                return 192;
            }();
            // v1.2's fb-aware anchor is OPT-IN after the 5-round gauntlet
            // caught it co-drifting: training vs the adjusted target made
            // fs_err SELF-REFERENTIAL (graded its own homework) — MER 19.5
            // with 100% RS fail, the liveness-law mirage. Needs a ground-
            // truth constraint (RS-fail feedback, E5) before default.
            static const bool DFE_ANCHOR = []() {
                const char* p = std::getenv("STVT_EQ_DFE_ANCHOR");
                return p && std::atoi(p) != 0;
            }();
            if (RLS_ENABLED) {
                adaptN_rls(data_mem, trn, data_mem2, KNOWN_FIELD_SYNC_LENGTH);
            } else if (DFE_G && !DFE_ANCHOR) {
                // v1.1 semantics: raw-target anchor + known-symbol flush
                adaptN(data_mem, trn, data_mem2, KNOWN_FIELD_SYNC_LENGTH);
                for (int k = 0; k < KNOWN_FIELD_SYNC_LENGTH; k++)
                    dfe_push(trn[k], NFB_G);
            } else if (DFE_G) {
                // DFE v1.2 FB-AWARE ANCHOR: the data path outputs
                // FFE·x − fb·hist, so training the FFE against raw trn
                // gives FFE and FBF FIGHTING objectives (v1.1's
                // intermittent healthy-channel instability). Fix without
                // touching adaptN: train against the ADJUSTED target
                // trn' = trn + fb·hist — then (FFE·x − fb·hist ≈ trn)
                // and both filters descend ONE objective. History
                // advances with the KNOWN symbols as we go (this also
                // replaces the separate post-adaptN flush).
                float trn_adj[KNOWN_FIELD_SYNC_LENGTH];
                for (int k = 0; k < KNOWN_FIELD_SYNC_LENGTH; k++) {
                    float fbv = 0.0f;
                    volk_32f_x2_dot_prod_32f(&fbv, &d_hist[d_hpos + 1],
                                             &d_fb[0], NFB_G);
                    trn_adj[k] = trn[k] + fbv;
                    dfe_push(trn[k], NFB_G);
                }
                adaptN(data_mem, trn_adj, data_mem2, KNOWN_FIELD_SYNC_LENGTH);
            } else if (d_flags & 0x0010) {
                adaptN(data_mem, training_sequence2, data_mem2, KNOWN_FIELD_SYNC_LENGTH);
            } else {
                adaptN(data_mem, training_sequence1, data_mem2, KNOWN_FIELD_SYNC_LENGTH);
            }
        } else {
            // Continuous decision-directed tracking on data segments (no-op
            // passive filter when STVT_EQ_DD_MU is unset/0).
            filterN_dd(data_mem, data_mem2, ATSC_DATA_SEGMENT_LENGTH);

            // ── MOD-12 GUARD (2026-07-07, the convicted mechanism's true
            // fix) ── each field = 312 data segments (26x12), so at every
            // FS the emitted count mod 12 must be 0. A discontinuity
            // breaks that and rotates the downstream viterbi's rigid
            // batch mapping (100% garbage, 1-in-12 remissions). Cure:
            // swallow <=11 segments (~1 ms) and the grid realigns —
            // self-heal within one field. STVT_EQ_MOD12_GUARD=1.
            static const bool MOD12_GUARD = []() {
                // DEFAULT ON since 2026-07-07 (5-round gauntlet: win
                // somewhere, regress nowhere). STVT_EQ_MOD12_GUARD=0
                // opts out.
                const char* p = std::getenv("STVT_EQ_MOD12_GUARD");
                return !(p && p[0] == '0');
            }();
            if (MOD12_GUARD && d_mod12_drop > 0) {
                d_mod12_drop--;               // consume, don't produce
            } else {
                memcpy(&out[output_produced * ATSC_DATA_SEGMENT_LENGTH],
                       data_mem2,
                       ATSC_DATA_SEGMENT_LENGTH * sizeof(float));

                plinfo pli_out(d_flags, d_segno);
                out_pl[output_produced++] = pli_out;
                d_mod12_count = (d_mod12_count + 1) % 12;
            }
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
