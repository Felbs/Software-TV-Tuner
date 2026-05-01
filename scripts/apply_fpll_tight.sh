#!/bin/bash
set -e
SRC="${GR_DTV_SRC:-/path/to/gnuradio/gr-dtv}"
DST=$HOME/gr-atscplus

cd $DST

cp $SRC/lib/atsc/atsc_fpll_impl.h        lib/atsc_fpll_tight_impl.h
cp $SRC/lib/atsc/atsc_fpll_impl.cc       lib/atsc_fpll_tight_impl.cc
cp $SRC/include/gnuradio/dtv/atsc_fpll.h include/gnuradio/atscplus/atsc_fpll_tight.h

# Public header
sed -i 's/INCLUDED_DTV_ATSC_FPLL_H/INCLUDED_ATSCPLUS_ATSC_FPLL_TIGHT_H/g; s|<gnuradio/dtv/api.h>|<gnuradio/atscplus/api.h>|g; s/namespace dtv/namespace atscplus/g; s/atsc_fpll\b/atsc_fpll_tight/g; s/DTV_API/ATSCPLUS_API/g' include/gnuradio/atscplus/atsc_fpll_tight.h

# Impl header
sed -i 's/INCLUDED_DTV_ATSC_FPLL_IMPL_H/INCLUDED_ATSCPLUS_ATSC_FPLL_TIGHT_IMPL_H/g; s|<gnuradio/dtv/atsc_fpll.h>|<gnuradio/atscplus/atsc_fpll_tight.h>|g; s/namespace dtv/namespace atscplus/g; s/atsc_fpll_impl\b/atsc_fpll_tight_impl/g; s/atsc_fpll\b/atsc_fpll_tight/g' lib/atsc_fpll_tight_impl.h

# Impl cc
sed -i 's|"atsc_fpll_impl.h"|"atsc_fpll_tight_impl.h"|g; s/namespace dtv/namespace atscplus/g; s/atsc_fpll_impl\b/atsc_fpll_tight_impl/g; s/atsc_fpll\b/atsc_fpll_tight/g' lib/atsc_fpll_tight_impl.cc

echo "=== fpll fork files in place ==="
ls lib/atsc_fpll_tight* include/gnuradio/atscplus/atsc_fpll_tight.h
