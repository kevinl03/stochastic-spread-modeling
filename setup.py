"""
setup.py — Build the C++ signal engine extension module.

Usage:
    python setup.py build_ext --inplace

The compiled module will be placed in the project root:
  - signal_engine.pyd   (Windows)
  - signal_engine.so    (macOS / Linux)
"""

import platform
import pybind11
from setuptools import Extension, setup

# Platform-specific compiler flags
if platform.system() == "Windows":
    compile_args = ["/O2", "/std:c++17", "/EHsc", "/DNDEBUG"]
    link_args = []
else:
    # macOS (clang) and Linux (gcc)
    compile_args = ["-O3", "-std=c++17", "-DNDEBUG", "-fvisibility=hidden"]
    link_args = []
    if platform.system() == "Darwin":
        # macOS: use libc++ and set minimum deployment target
        compile_args += ["-stdlib=libc++", "-mmacosx-version-min=11.0"]
        link_args += ["-stdlib=libc++", "-mmacosx-version-min=11.0"]

ext = Extension(
    "signal_engine",
    sources=["cpp/signal_engine.cpp"],
    include_dirs=[pybind11.get_include()],
    language="c++",
    extra_compile_args=compile_args,
    extra_link_args=link_args,
)

setup(
    name="signal_engine",
    version="1.0.0",
    description="C++ signal computation for mean-reversion spread trading",
    ext_modules=[ext],
)
