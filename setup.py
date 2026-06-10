"""
setup.py — Build the C++ signal engine extension module.

Usage:
    python setup.py build_ext --inplace

The compiled module (signal_engine.pyd on Windows) will be placed in
the project root, importable as `import signal_engine`.
"""

import pybind11
from setuptools import Extension, setup

ext = Extension(
    "signal_engine",
    sources=["cpp/signal_engine.cpp"],
    include_dirs=[pybind11.get_include()],
    language="c++",
    extra_compile_args=["/O2", "/std:c++17", "/EHsc", "/DNDEBUG"],
)

setup(
    name="signal_engine",
    version="0.1.0",
    description="C++ signal computation for mean-reversion spread trading",
    ext_modules=[ext],
)
