#!/bin/bash
set -e
SRC="${GR_DTV_SRC:-/path/to/gnuradio/gr-dtv}"
DST=$HOME/gr-atscplus

cd $DST

cp $SRC/lib/atsc/atsc_sync_impl.h        lib/atsc_sync_tunable_impl.h
cp $SRC/lib/atsc/atsc_sync_impl.cc       lib/atsc_sync_tunable_impl.cc
cp $SRC/include/gnuradio/dtv/atsc_sync.h include/gnuradio/atscplus/atsc_sync_tunable.h

# Public header
sed -i 's/INCLUDED_DTV_ATSC_SYNC_H/INCLUDED_ATSCPLUS_ATSC_SYNC_TUNABLE_H/g; s|<gnuradio/dtv/api.h>|<gnuradio/atscplus/api.h>|g; s/namespace dtv/namespace atscplus/g; s/atsc_sync\b/atsc_sync_tunable/g; s/DTV_API/ATSCPLUS_API/g' include/gnuradio/atscplus/atsc_sync_tunable.h

# Impl header
sed -i 's/INCLUDED_DTV_ATSC_SYNC_IMPL_H/INCLUDED_ATSCPLUS_ATSC_SYNC_TUNABLE_IMPL_H/g; s|<gnuradio/dtv/atsc_sync.h>|<gnuradio/atscplus/atsc_sync_tunable.h>|g; s/namespace dtv/namespace atscplus/g; s/atsc_sync_impl\b/atsc_sync_tunable_impl/g; s/atsc_sync\b/atsc_sync_tunable/g' lib/atsc_sync_tunable_impl.h

# Impl cc
sed -i 's|"atsc_sync_impl.h"|"atsc_sync_tunable_impl.h"|g; s/namespace dtv/namespace atscplus/g; s/atsc_sync_impl\b/atsc_sync_tunable_impl/g; s/atsc_sync\b/atsc_sync_tunable/g' lib/atsc_sync_tunable_impl.cc

echo "=== sync fork files in place ==="
ls lib/atsc_sync_tunable* include/gnuradio/atscplus/atsc_sync_tunable.h
