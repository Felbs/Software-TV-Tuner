find_package(PkgConfig)

PKG_CHECK_MODULES(PC_GR_ATSCPLUS gnuradio-atscplus)

FIND_PATH(
    GR_ATSCPLUS_INCLUDE_DIRS
    NAMES gnuradio/atscplus/api.h
    HINTS $ENV{ATSCPLUS_DIR}/include
        ${PC_ATSCPLUS_INCLUDEDIR}
    PATHS ${CMAKE_INSTALL_PREFIX}/include
          /usr/local/include
          /usr/include
)

FIND_LIBRARY(
    GR_ATSCPLUS_LIBRARIES
    NAMES gnuradio-atscplus
    HINTS $ENV{ATSCPLUS_DIR}/lib
        ${PC_ATSCPLUS_LIBDIR}
    PATHS ${CMAKE_INSTALL_PREFIX}/lib
          ${CMAKE_INSTALL_PREFIX}/lib64
          /usr/local/lib
          /usr/local/lib64
          /usr/lib
          /usr/lib64
          )

include("${CMAKE_CURRENT_LIST_DIR}/gnuradio-atscplusTarget.cmake")

INCLUDE(FindPackageHandleStandardArgs)
FIND_PACKAGE_HANDLE_STANDARD_ARGS(GR_ATSCPLUS DEFAULT_MSG GR_ATSCPLUS_LIBRARIES GR_ATSCPLUS_INCLUDE_DIRS)
MARK_AS_ADVANCED(GR_ATSCPLUS_LIBRARIES GR_ATSCPLUS_INCLUDE_DIRS)
