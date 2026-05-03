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
#include <cstdlib>
#include <cstdio>
#include <cstring>

namespace {
// Tier-16: helper to read float-valued env var with default.
static double read_env_double(const char* name, double dflt) {
    const char* s = std::getenv(name);
    if (!s || !*s) return dflt;
    char* end = nullptr;
    double v = std::strtod(s, &end);
    if (end == s) return dflt;
    return v;
}

// Tier-15: try to load `n_taps` IEEE-754 little-endian float32 values from a
// pre-baked file containing channel-inverse equalizer taps. Returns true and
// fills `out` on success; returns false (leaving `out` untouched) if the file
// cannot be opened, is the wrong size, or contains non-finite values. Order
// of search:
//   1. ATSCPLUS_WARM_START_IR env var (full path)
//   2. C:\\Users\\user\\radioconda\\Library\\share\\atscplus\\warm_start_ir.f32
// If neither is available, callers should fall back to the existing delta
// init.
static const char* tier15_default_warm_start_path()
{
    return "C:\\Users\\user\\radioconda\\Library\\share\\atscplus\\warm_start_ir.f32";
}

static bool tier15_load_warm_start_taps(int n_taps, float* out)
{
    const char* env_path = std::getenv("ATSCPLUS_WARM_START_IR");
    const char* path = (env_path && *env_path)
                           ? env_path
                           : tier15_default_warm_start_path();

    // Allow explicit disable: if env var is "0" or "off", do not attempt load.
    if (env_path && (std::strcmp(env_path, "0") == 0 ||
                      std::strcmp(env_path, "off") == 0 ||
                      std::strcmp(env_path, "OFF") == 0)) {
        std::fprintf(stderr,
            "[eq_long tier15] warm-start disabled by env (ATSCPLUS_WARM_START_IR=%s)\n",
            env_path);
        std::fflush(stderr);
        return false;
    }

    std::ifstream f(path, std::ios::binary);
    if (!f) {
        std::fprintf(stderr,
            "[eq_long tier15] warm-start file not found at '%s'; using delta init\n",
            path);
        std::fflush(stderr);
        return false;
    }
    f.seekg(0, std::ios::end);
    std::streamoff sz = f.tellg();
    f.seekg(0, std::ios::beg);
    const std::streamoff expected = (std::streamoff)n_taps * (std::streamoff)sizeof(float);
    if (sz != expected) {
        std::fprintf(stderr,
            "[eq_long tier15] warm-start file '%s' size=%lld expected=%lld; using delta init\n",
            path, (long long)sz, (long long)expected);
        std::fflush(stderr);
        return false;
    }
    if (!f.read(reinterpret_cast<char*>(out), expected)) {
        std::fprintf(stderr,
            "[eq_long tier15] warm-start file '%s' read failed; using delta init\n",
            path);
        std::fflush(stderr);
        return false;
    }
    // Validate: all finite.
    double l2sq = 0.0;
    float maxabs = 0.0f;
    int   maxidx = 0;
    for (int i = 0; i < n_taps; i++) {
        if (!std::isfinite(out[i])) {
            std::fprintf(stderr,
                "[eq_long tier15] warm-start tap %d non-finite; using delta init\n", i);
            std::fflush(stderr);
            return false;
        }
        l2sq += (double)out[i] * (double)out[i];
        float a = std::fabs(out[i]);
        if (a > maxabs) { maxabs = a; maxidx = i; }
    }
    std::fprintf(stderr,
        "[eq_long tier15] warm-start loaded from '%s'  n_taps=%d  l2=%.4f  "
        "max=%.4f@%d\n",
        path, n_taps, std::sqrt(l2sq), maxabs, maxidx);
    std::fflush(stderr);
    return true;
}
}

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
    // Tier-15 (2026-05-02): try warm-start from a pre-baked channel-inverse
    // file before falling back to delta init. The file is generated by
    // `_tier15_bake_warmstart.py` from the Tier-6 channel IR estimate.
    // Hypothesis: starting taps near the channel inverse skips the
    // ~30 s LMS discovery phase, lands in a more stable basin, fewer drift
    // events. Tier-3 measured healthy-lock ||taps|| ≈ 1.5; the baked
    // taps have ||·|| ≈ 1.25 with tap[NPRETAPS]=1.0 by construction, so
    // delta-init's invariants are preserved. If load fails (file missing,
    // wrong size, corrupt), fall back to delta init silently.
    if (!tier15_load_warm_start_taps(NTAPS, &d_taps[0])) {
        for (int k = 0; k < NTAPS; k++) d_taps[k] = 0.0f;
        d_taps[NPRETAPS] = 1.0f; // delta-function init — equalizer starts as pass-through
    }

    // Tier-16: read sweep parameters from environment. Defaults reproduce the
    // currently-shipped Tier-3+Tier-10 values.
    d_magic_beta     = read_env_double("MAGIC_BETA",     5e-5);
    d_magic_leak     = (float)read_env_double("MAGIC_LEAK",     5e-4);
    d_magic_div_bail = (float)read_env_double("MAGIC_DIV_BAIL", 50.0);
    std::fprintf(stderr,
        "[eq_long tier16] BETA=%.6g LEAK=%.6g DIV_BAIL=%.6g\n",
        d_magic_beta, (double)d_magic_leak, (double)d_magic_div_bail);
    std::fflush(stderr);

    // Tier-20: per-FS-interval data-segment dev_rms telemetry. Gated default-OFF
    // so production behavior is unchanged. Set ATSCPLUS_TIER20_LOG=1 to enable.
    {
        const char* s = std::getenv("ATSCPLUS_TIER20_LOG");
        d_tier20_log_enabled = (s && *s &&
                                std::strcmp(s, "0") != 0 &&
                                std::strcmp(s, "off") != 0 &&
                                std::strcmp(s, "OFF") != 0);
        if (d_tier20_log_enabled) {
            d_tier20_t0 = std::chrono::steady_clock::now();
            // CSV header line — once at construction.
            std::fprintf(stderr,
                "[tier20] CSV_HEADER fs_pass_idx,wall_time_sec,tap_norm,"
                "fs_mse,data_dev_rms,data_dev_max,n_data_segs\n");
            std::fflush(stderr);
        }
    }

    const int alignment_multiple = volk_get_alignment() / sizeof(float);
    set_alignment(std::max(1, alignment_multiple));
}

// Tier-20: accumulate one data-segment's dev_rms and max-update, where dev_rms
// is sqrt(mean((y_n - 1.25 - slice_8vsb(y_n - 1.25))^2)) over the segment's
// 832 samples. The 1.25 offset is the post-pilot DC bias on 8-VSB after the
// pilot tone is cancelled by the FPLL/sync chain.
void atsc_equalizer_long_impl::tier20_accumulate_data_seg(const float* y, int nsamples)
{
    // Tier-20 slicer: the brief specifies a 1.25 post-pilot DC offset to be
    // subtracted before slicing. We auto-pick the offset from the segment mean
    // — if the empirical mean is in [0.5, 2.0] the pilot is still present, so
    // subtract the brief's 1.25 (matches the stated mode). If the empirical
    // mean is near 0 the pilot has already been cancelled upstream and we
    // slice without offset. Either way, the trend over time is the same; this
    // just makes the absolute dev_rms numbers comparable to Tier-18.
    double mean = 0.0;
    for (int j = 0; j < nsamples; j++) mean += (double)y[j];
    mean /= (nsamples > 0 ? nsamples : 1);
    float offset = 0.0f;
    if (mean > 0.5 && mean < 2.0) offset = 1.25f;

    double sse = 0.0;
    for (int j = 0; j < nsamples; j++) {
        float v = y[j] - offset;
        // Slice to nearest of {-7,-5,-3,-1,1,3,5,7}.
        float dec;
        if      (v >=  6.0f) dec =  7.0f;
        else if (v >=  4.0f) dec =  5.0f;
        else if (v >=  2.0f) dec =  3.0f;
        else if (v >=  0.0f) dec =  1.0f;
        else if (v >= -2.0f) dec = -1.0f;
        else if (v >= -4.0f) dec = -3.0f;
        else if (v >= -6.0f) dec = -5.0f;
        else                 dec = -7.0f;
        float e = v - dec;
        sse += (double)e * (double)e;
    }
    float dev_rms = (nsamples > 0) ? (float)std::sqrt(sse / (double)nsamples) : 0.0f;
    d_tier20_dev_rms_sum += (double)dev_rms;
    if (dev_rms > d_tier20_dev_rms_max) d_tier20_dev_rms_max = dev_rms;
    // Track the segment-mean too (debug only) — store in dev_max if not larger.
    // Actually we keep dev_max for max dev_rms across segments — leave as is.
    d_tier20_data_seg_count++;
}

// Tier-20: emit one CSV-friendly stderr line at every FS pass, summarizing
// the data segments observed since the previous FS. Resets the running
// accumulators afterwards.
void atsc_equalizer_long_impl::tier20_emit_fs_line(float tap_norm)
{
    auto now = std::chrono::steady_clock::now();
    double wall = std::chrono::duration<double>(now - d_tier20_t0).count();
    int n = d_tier20_data_seg_count;
    float dev_rms = (n > 0) ? (float)(d_tier20_dev_rms_sum / (double)n) : 0.0f;
    float dev_max = d_tier20_dev_rms_max;
    std::fprintf(stderr,
        "[tier20] %d,%.6f,%.6f,%.6f,%.6f,%.6f,%d\n",
        d_tier20_fs_pass_idx,
        wall,
        (double)tap_norm,
        (double)d_last_fs_mse,
        (double)dev_rms,
        (double)dev_max,
        n);
    std::fflush(stderr);

    d_tier20_fs_pass_idx++;
    d_tier20_data_seg_count = 0;
    d_tier20_dev_rms_sum    = 0.0;
    d_tier20_dev_rms_max    = 0.0f;
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
    // Tier-16: read at construction time from MAGIC_BETA / MAGIC_LEAK /
    // MAGIC_DIV_BAIL env vars. Defaults match shipped Tier-3 values.
    const double BETA = d_magic_beta;
    const float  LEAK = d_magic_leak;
    const float  DIVERGENCE_BAIL = d_magic_div_bail;

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
            // Tier-20: emit per-FS CSV line summarizing the data segments
            // accumulated since the previous FS. tap_norm computed live to
            // capture the post-leakage / post-anti-windup state.
            if (d_tier20_log_enabled) {
                double tap_e = 0.0;
                for (int k = 0; k < NTAPS; k++) tap_e += (double)d_taps[k] * (double)d_taps[k];
                tier20_emit_fs_line((float)std::sqrt(tap_e));
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

            // Tier-20: accumulate per-data-segment dev_rms (post-equalizer,
            // post-1.25 mean-offset, vs nearest 8-VSB rail). No-op if logging
            // gated off — tier20_log_enabled cheap-checked first.
            if (d_tier20_log_enabled) {
                tier20_accumulate_data_seg(data_mem2, ATSC_DATA_SEGMENT_LENGTH);
            }

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
