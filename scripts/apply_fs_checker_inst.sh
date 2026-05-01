#!/bin/bash
set -e
SRC="${GR_DTV_SRC:-/path/to/gnuradio/gr-dtv}"
DST=$HOME/gr-atscplus

cd $DST

cp $SRC/lib/atsc/atsc_fs_checker_impl.h        lib/atsc_fs_checker_inst_impl.h
cp $SRC/lib/atsc/atsc_fs_checker_impl.cc       lib/atsc_fs_checker_inst_impl.cc
cp $SRC/include/gnuradio/dtv/atsc_fs_checker.h include/gnuradio/atscplus/atsc_fs_checker_inst.h

# Public header
sed -i 's/INCLUDED_DTV_ATSC_FS_CHECKER_H/INCLUDED_ATSCPLUS_ATSC_FS_CHECKER_INST_H/g; s|<gnuradio/dtv/api.h>|<gnuradio/atscplus/api.h>|g; s/namespace dtv/namespace atscplus/g; s/atsc_fs_checker\b/atsc_fs_checker_inst/g; s/DTV_API/ATSCPLUS_API/g' include/gnuradio/atscplus/atsc_fs_checker_inst.h

# Impl header (only namespace + class name swap; keep gr::dtv types via using)
sed -i 's/INCLUDED_DTV_ATSC_FS_CHECKER_IMPL_H/INCLUDED_ATSCPLUS_ATSC_FS_CHECKER_INST_IMPL_H/g; s|<gnuradio/dtv/atsc_fs_checker.h>|<gnuradio/atscplus/atsc_fs_checker_inst.h>|g; s/namespace dtv/namespace atscplus/g; s/atsc_fs_checker_impl\b/atsc_fs_checker_inst_impl/g; s/atsc_fs_checker\b/atsc_fs_checker_inst/g' lib/atsc_fs_checker_inst_impl.h

# Impl cc namespace + class swap (instrumentation done by Write tool after this)
sed -i 's|"atsc_fs_checker_impl.h"|"atsc_fs_checker_inst_impl.h"|g; s/namespace dtv/namespace atscplus/g; s/atsc_fs_checker_impl\b/atsc_fs_checker_inst_impl/g; s/atsc_fs_checker\b/atsc_fs_checker_inst/g; s|"atsc_pnXXX_impl.h"|<atsc/atsc_pnXXX_impl.h>|g; s|"atsc_syminfo_impl.h"|<atsc/atsc_syminfo_impl.h>|g; s|"atsc_types.h"|<atsc/atsc_types.h>|g' lib/atsc_fs_checker_inst_impl.cc

echo "=== files in place ==="
ls lib/atsc_fs_checker_inst* include/gnuradio/atscplus/atsc_fs_checker_inst.h
