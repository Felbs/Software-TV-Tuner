/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */

#ifndef INCLUDED_ATSCPLUS_ATSC_ADAPTIVE_NOTCH_H
#define INCLUDED_ATSCPLUS_ATSC_ADAPTIVE_NOTCH_H

#include <gnuradio/atscplus/api.h>
#include <gnuradio/sync_block.h>

namespace gr {
namespace atscplus {

/*!
 * \brief Adaptive narrowband interferer notch.
 *
 * Periodically takes an FFT of input samples, finds the strongest
 * single-frequency peak above a median-relative threshold, and notches
 * that frequency with a sharp complex IIR. Updates the notch frequency
 * as the dominant interferer changes.
 *
 * Inserted in the chain as:
 *   src -> scaler -> [noise_blanker ->] [notch ->] resamp -> rxf -> fpll
 *
 * The pilot tone at ~-2.69 MHz is excluded from peak detection so we
 * don't accidentally notch the ATSC carrier-recovery signal.
 *
 * Disabled by default (STVT_NOTCH=0). When enabled, the block passes
 * input straight through until a peak above threshold is found.
 *
 * \ingroup atscplus_rf
 */
class ATSCPLUS_API atsc_adaptive_notch : virtual public gr::sync_block
{
public:
    typedef std::shared_ptr<atsc_adaptive_notch> sptr;

    /*!
     * \brief Make an adaptive notch filter.
     *
     * \param sample_rate    Input/output sample rate in Hz. Used to map
     *                       FFT bin indices to absolute frequencies.
     * \param fft_size       FFT size for peak detection. Power-of-2
     *                       in [256, 8192]. Larger = finer frequency
     *                       resolution but slower update. Default 1024.
     * \param threshold_db   Peak detection threshold above bin-median,
     *                       in dB. Below this, no notch is applied.
     *                       Default 12 dB.
     * \param pole_radius    IIR notch sharpness (0 < r < 1, closer to
     *                       1 = sharper but slower transient response).
     *                       Default 0.985.
     * \param pilot_offset_hz  Frequency to EXCLUDE from peak detection
     *                       (the ATSC pilot tone). Default -2.69e6.
     * \param pilot_guard_hz Width of the pilot exclusion window.
     *                       Default 200 kHz (each side).
     */
    static sptr make(double sample_rate,
                     int    fft_size       = 1024,
                     double threshold_db   = 12.0,
                     double pole_radius    = 0.985,
                     double pilot_offset_hz = -2.69e6,
                     double pilot_guard_hz =  200e3);

    /* Telemetry. */
    virtual uint64_t num_samples()       const = 0;
    virtual uint64_t num_notch_active()  const = 0;
    virtual double   current_notch_hz()  const = 0;
};

} /* namespace atscplus */
} /* namespace gr */

#endif
