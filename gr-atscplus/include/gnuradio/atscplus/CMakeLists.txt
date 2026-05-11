# Install public headers
install(FILES
    api.h
    atsc_equalizer_long.h
    atsc_equalizer_pilot.h
    atsc_equalizer_pilot_dd.h
    atsc_equalizer_pilot_dd_soft.h
    atsc_equalizer_pilot_multifs.h
    atsc_equalizer_pilot_multifs_dd.h
    atsc_equalizer_cma.h
    atsc_fs_checker_inst.h
    atsc_fpll_tight.h
    atsc_sync_tunable.h
    atsc_sync_soft.h
    atsc_sync_kalman.h
    atsc_sync_fieldlock.h
    atsc_sync_slidefs.h
    atsc_sync_pathA.h
    atsc_viterbi_soft.h
    DESTINATION include/gnuradio/atscplus
)
