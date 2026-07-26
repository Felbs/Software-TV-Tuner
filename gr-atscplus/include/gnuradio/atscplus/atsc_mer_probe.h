/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */

#ifndef INCLUDED_ATSCPLUS_ATSC_MER_PROBE_H
#define INCLUDED_ATSCPLUS_ATSC_MER_PROBE_H

#include <gnuradio/atscplus/api.h>
#include <gnuradio/sync_block.h>
#include <cstdint>

namespace gr {
namespace atscplus {

/*!
 * \brief Decision-directed MER meter on the equalized symbol stream.
 *
 * A cheap C++ sink that taps ANY equalizer's soft-symbol output (a stream of
 * 832-float ATSC data segments) and reports a Modulation Error Ratio derived
 * from the 8-VSB decision error, so a live MER exists even on the stock
 * gr-dtv equalizer (which emits no fs_err_rms of its own).
 *
 * Each symbol is sliced to the nearest 8-VSB level {+-1,+-3,+-5,+-7} after a
 * slow-EWMA DC-removal + gain-normalization (self-calibrates to whatever
 * scale / pilot-DC a given equalizer emits, without absorbing the fast error
 * we want to see). Every `period` symbols it prints to stderr:
 *   [mer-probe TAG] dd_err_rms=X.XXXX dd_mer=YY.YY
 * where dd_mer = 20*log10(5/dd_err_rms), matching the fs_err_rms convention of
 * atsc_equalizer_long. Validated to 0.01 dB against that real MER on `long`.
 *
 * Pure measurement — a sink, so it never touches the decode path. Being C++
 * (no Python GIL) it keeps up with the full symbol rate on a CPU-bound host,
 * unlike the earlier embedded-Python probe which throttled the decode.
 *
 * Wire it as an extra fan-out consumer of the equalizer's port 0:
 *   self.connect((equalizer, 0), mer_probe)
 *
 * \ingroup atscplus
 */
class ATSCPLUS_API atsc_mer_probe : virtual public gr::sync_block
{
public:
    typedef std::shared_ptr<atsc_mer_probe> sptr;

    /*!
     * \brief Make a MER probe.
     * \param tag     Label printed in the telemetry line (e.g. the channel).
     * \param period  Symbols between emissions (default ~5e6 ≈ once/0.5s).
     */
    static sptr make(const std::string& tag = "eq", uint64_t period = 5000000);

    /* Latest computed values (telemetry / test access). */
    virtual double dd_err_rms() const = 0;
    virtual double dd_mer()     const = 0;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_MER_PROBE_H */
