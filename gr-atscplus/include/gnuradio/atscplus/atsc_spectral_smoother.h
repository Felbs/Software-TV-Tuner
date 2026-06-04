/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */

#ifndef INCLUDED_ATSCPLUS_ATSC_SPECTRAL_SMOOTHER_H
#define INCLUDED_ATSCPLUS_ATSC_SPECTRAL_SMOOTHER_H

#include <gnuradio/atscplus/api.h>
#include <gnuradio/sync_block.h>

namespace gr {
namespace atscplus {

/*!
 * \brief FFT-domain spectral outlier suppression.
 *
 * For each non-overlapping block of FFT_SIZE samples:
 *   1. FFT input block
 *   2. For each bin k, compute neighborhood reference = median of
 *      |X[k-N..k+N]| (excluding bin k itself).
 *   3. If |X[k]| > THRESHOLD × neighborhood_ref, smoothly pull |X[k]|
 *      down to THRESHOLD × neighborhood_ref (preserves phase).
 *   4. IFFT and emit output block.
 *
 * Unlike a recursive IIR notch, this is stateless per-block — no
 * transients on activation/deactivation, no ringing when the
 * outlier moves frequencies. The smooth proportional pull-down
 * means weak peaks are barely touched while strong outliers are
 * strongly attenuated, with no on/off discontinuity.
 *
 * Inserted in the chain as:
 *   ... -> resamp -> [spectral_smoother ->] rxf -> fpll -> ...
 *
 * Disabled by default (STVT_SPECTRAL=0). When enabled, latency
 * is FFT_SIZE / sample_rate (e.g., 1024/6.25e6 = 0.16 ms).
 *
 * \ingroup atscplus_rf
 */
class ATSCPLUS_API atsc_spectral_smoother : virtual public gr::sync_block
{
public:
    typedef std::shared_ptr<atsc_spectral_smoother> sptr;

    /*!
     * \brief Make a new spectral smoother.
     *
     * \param sample_rate      Input/output sample rate (Hz). Used for
     *                         telemetry only; the math is sample-rate
     *                         agnostic.
     * \param fft_size         Block size for FFT. Power-of-2 in
     *                         [256, 8192]. Default 1024.
     * \param neighborhood     Number of bins on each side used for the
     *                         local-median reference. Default 32.
     * \param threshold        Outlier ratio; bins above THRESHOLD ×
     *                         local_median are pulled down. Default 3.0.
     */
    static sptr make(double sample_rate,
                     int    fft_size       = 1024,
                     int    neighborhood   = 32,
                     double threshold      = 3.0,
                     double pilot_offset_hz = -2.69e6,
                     double pilot_guard_hz  =  300e3);

    /* Telemetry. */
    virtual uint64_t num_samples()        const = 0;
    virtual uint64_t num_blocks()         const = 0;
    virtual uint64_t num_bins_suppressed() const = 0;
};

} /* namespace atscplus */
} /* namespace gr */

#endif
