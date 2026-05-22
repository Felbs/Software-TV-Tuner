/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */

#ifndef INCLUDED_ATSCPLUS_ATSC_NOISE_BLANKER_H
#define INCLUDED_ATSCPLUS_ATSC_NOISE_BLANKER_H

#include <gnuradio/atscplus/api.h>
#include <gnuradio/sync_block.h>

namespace gr {
namespace atscplus {

/*!
 * \brief Wide-band impulse noise blanker.
 *
 * Tracks an EMA of |sample| amplitude and clips samples whose magnitude
 * exceeds `threshold * EMA`, blanking them and the following
 * `blank_samples` samples to suppress impulse-noise sources (lightning,
 * ignition, electrical arcing) BEFORE they reach the matched filter and
 * equalizer.
 *
 * Inserted in the chain as:
 *   src -> scaler -> noise_blanker -> resamp -> rxf -> fpll -> ...
 *
 * Disabled by default (threshold <= 0 = no blanking, pure passthrough).
 *
 * \ingroup atscplus_rf
 */
class ATSCPLUS_API atsc_noise_blanker : virtual public gr::sync_block
{
public:
    typedef std::shared_ptr<atsc_noise_blanker> sptr;

    /*!
     * \brief Make a new noise blanker.
     *
     * \param threshold      Impulse trigger (sample magnitude > threshold*EMA).
     *                       Set <=0 to disable blanking. Default 3.0.
     * \param blank_samples  How many samples to blank after a trigger.
     *                       Higher = more aggressive impulse coverage but
     *                       more signal loss. Default 8.
     * \param alpha          EMA rate for amplitude tracking. 0 < alpha < 1.
     *                       Smaller = more stable (slower to adapt to true
     *                       level changes). Default 1e-4.
     */
    static sptr make(float threshold = 3.0f,
                     int blank_samples = 8,
                     float alpha = 1e-4f);

    /* Telemetry. */
    virtual uint64_t num_samples()  const = 0;
    virtual uint64_t num_blanked()  const = 0;
};

} /* namespace atscplus */
} /* namespace gr */

#endif