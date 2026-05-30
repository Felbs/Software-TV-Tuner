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
