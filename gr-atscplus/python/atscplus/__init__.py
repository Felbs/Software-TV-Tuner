#
# Copyright 2008,2009 Free Software Foundation, Inc.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#

# The presence of this file turns this directory into a Python package

'''
This is the GNU Radio HOWTO module. Place your Python package
description here (python/__init__.py).
'''
import os

# import pybind11 generated symbols. The compiled module is named
# atscplus_python on modern builds (cookiecutter renamed in gr-modtool
# >= 3.10), but earlier builds and Windows radioconda installs may
# still produce howto_python from the legacy template. Try the modern
# name first, fall back to the legacy one. Either way, an unbuilt
# python-only checkout has neither — that's also fine.
try:
    from .atscplus_python import *
except ModuleNotFoundError:
    try:
        from .howto_python import *
    except ModuleNotFoundError:
        pass

# import any pure python here
#
