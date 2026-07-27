#!/usr/bin/env python3
"""Patch MobiPkg/Compile's example/libvips config to build a fuller libvips.

MobiPkg already knows how to cross-compile glib + libvips for Android (that is
the hard part, and it is solved there). Its default config, however, is a
"web" build: JPEG/PNG/WebP only, and crucially `-Dcplusplus=false`, so it does
NOT produce libvips-cpp (which sharp requires).

This script rewrites, in-place, two files under the given MobiPkg
example/libvips directory:

  * workspace.yaml    - add the extra format dependency libs so the workspace
                        builds them before libvips.
  * libvips/lib.yaml  - enable C++ binding + the extra format options, and add
                        the matching deps entries.

Phase 1 (this commit) intentionally adds only:
  * cplusplus  (required by sharp; gives libvips-cpp.so)
  * tiff       (libtiff, deps: zlib only -> low risk, validates the approach)

HEIF/AVIF/JXL/etc. are added in a later phase once this pipeline is green,
because their dep chains (libde265/aom/highway) are heavier.

Args:
  argv[1]  path to MobiPkg example/libvips dir
Env:
  IV_FORMATS  comma list to enable (default "tiff"); recognised tokens:
              tiff, heif, openjpeg, lcms, exif, cgif
"""

import os
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

if len(sys.argv) < 2:
    sys.exit("usage: _patch-mobipkg-libvips.py <example/libvips dir>")

BASE = sys.argv[1]
WORKSPACE = os.path.join(BASE, "workspace.yaml")
LIBVIPS = os.path.join(BASE, "libvips", "lib.yaml")

for p in (WORKSPACE, LIBVIPS):
    if not os.path.isfile(p):
        sys.exit(f"missing: {p}")

formats = [f.strip() for f in os.environ.get("IV_FORMATS", "tiff").split(",") if f.strip()]

# Map format token -> (workspace lib entries, libvips dep names, meson options)
# workspace entries are (name, path) added under libs:, deps go into libvips
# deps:, options replace the matching -D line.
FORMAT_MAP = {
    "tiff": {
        "libs": [("libtiff", "deps/libtiff")],
        "deps": ["libtiff"],
        "options": {"tiff": "enabled"},
    },
    "heif": {
        # libheif needs libde265 built first
        "libs": [("libde265", "deps/libde265"), ("libheif", "deps/libheif")],
        "deps": ["libde265", "libheif"],
        "options": {"heif": "enabled"},
    },
    "openjpeg": {
        "libs": [("openjpeg", "deps/openjpeg")],
        "deps": ["openjpeg"],
        "options": {"openjpeg": "enabled"},
    },
    "lcms": {
        "libs": [("lcms2", "deps/lcms2")],
        "deps": ["lcms2"],
        "options": {"lcms": "enabled"},
    },
    "exif": {
        "libs": [("libexif", "deps/libexif")],
        "deps": ["libexif"],
        "options": {"exif": "enabled"},
    },
}

# ---- 1) workspace.yaml : append the needed libs before libvips -------------
with open(WORKSPACE, encoding="utf-8") as f:
    ws = yaml.safe_load(f)

ws_libs = ws.get("libs", [])
existing_names = {l["name"] for l in ws_libs}

# Find libvips index so new deps are inserted before it.
vips_idx = next((i for i, l in enumerate(ws_libs) if l["name"] == "libvips"), len(ws_libs))

to_insert = []
for fmt in formats:
    spec = FORMAT_MAP.get(fmt)
    if not spec:
        print(f"WARN: unknown format '{fmt}', skipping")
        continue
    for name, path in spec["libs"]:
        if name not in existing_names:
            to_insert.append({"name": name, "path": path})
            existing_names.add(name)

ws_libs[vips_idx:vips_idx] = to_insert
ws["libs"] = ws_libs

with open(WORKSPACE, "w", encoding="utf-8") as f:
    yaml.safe_dump(ws, f, sort_keys=False, allow_unicode=True)
print(f"workspace.yaml: inserted libs {[l['name'] for l in to_insert]}")

# ---- 1b) per-dep flag workarounds ------------------------------------------
# Some dep recipes enable -Werror while MobiPkg injects -L into the *compile*
# command line (harmless but unused at compile time), tripping
# -Werror,-Wunused-command-line-argument. -Qunused-arguments silences exactly
# that clang diagnostic without weakening real error checking.
DEP_FLAG_PATCHES = {
    "libheif": {"c": "-Qunused-arguments", "cxx": "-Qunused-arguments"},
}

# Some MobiPkg dep recipes have the wrong build type. libexif v0.6.24 ships only
# autotools (configure.ac / Makefile.am, no CMakeLists.txt), yet its recipe says
# `type: cmake`, so cmake aborts with "does not appear to contain CMakeLists.txt".
# Rewrite the whole recipe body for such deps to a correct autotools build.
DEP_RECIPE_OVERRIDES = {
    # lcms2's recipe passes -Ddefault_library=static, but MobiPkg's meson wrapper
    # already injects --default-library=both. meson rejects the duplicated builtin
    # option, so drop it and let MobiPkg drive the library type.
    "lcms2": {
        "name": "lcms2",
        "type": "meson",
        "source": {
            "git": {
                "url": "https://github.com/mm2/Little-CMS.git",
                "ref": "lcms2.16",
            }
        },
        "license": "LICENSE",
        "options": [
            "-Dutils=false",
            "-Dsamples=false",
        ],
    },
    "libexif": {
        "name": "libexif",
        "type": "autotools",
        "source": {
            "git": {
                "url": "https://github.com/libexif/libexif.git",
                "ref": "v0.6.24",
            }
        },
        "license": "COPYING",
        "flags": {
            "c": "-fPIC -O2",
            "cxx": "-fPIC -O2",
            "cpp": "",
            "ld": "",
        },
        # git checkout has no ./configure yet; regenerate the autotools scripts.
        "precompile": ["autoreconf -fiv"],
        "options": [
            "--enable-static",
            "--disable-shared",
            "--disable-docs",
            "--disable-nls",
        ],
    },
}


def override_dep_recipe(dep_path, recipe):
    """Overwrite a dep's lib.yaml wholesale (idempotent by content)."""
    p = os.path.join(BASE, dep_path, "lib.yaml")
    if not os.path.isfile(p):
        print(f"WARN: dep lib.yaml missing, cannot override recipe: {p}")
        return
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(recipe, f, sort_keys=False, allow_unicode=True)
    print(f"{dep_path}/lib.yaml: recipe overridden (type={recipe.get('type')})")


def patch_dep_flags(dep_path, extra):
    """Append extra c/cxx flags to a dep's lib.yaml (idempotent)."""
    p = os.path.join(BASE, dep_path, "lib.yaml")
    if not os.path.isfile(p):
        print(f"WARN: dep lib.yaml missing, cannot patch flags: {p}")
        return
    with open(p, encoding="utf-8") as f:
        d = yaml.safe_load(f)
    flags = d.get("flags") or {}
    for key, add in extra.items():
        cur = flags.get(key) or ""
        if add not in cur.split():
            flags[key] = (cur + " " + add).strip()
    d["flags"] = flags
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(d, f, sort_keys=False, allow_unicode=True)
    print(f"{dep_path}/lib.yaml: flags += {extra}")


# Only patch deps that are actually being built for the requested formats.
_dep_paths = {name: path for fmt in formats if FORMAT_MAP.get(fmt)
              for name, path in FORMAT_MAP[fmt]["libs"]}
for name, extra in DEP_FLAG_PATCHES.items():
    if name in _dep_paths:
        patch_dep_flags(_dep_paths[name], extra)

# Apply full recipe overrides for deps with a wrong build type.
for name, recipe in DEP_RECIPE_OVERRIDES.items():
    if name in _dep_paths:
        override_dep_recipe(_dep_paths[name], recipe)

# ---- 2) libvips/lib.yaml : enable cplusplus + formats + deps ---------------
with open(LIBVIPS, encoding="utf-8") as f:
    lv = yaml.safe_load(f)

# 2a) deps
lv_deps = lv.get("deps", [])
for fmt in formats:
    spec = FORMAT_MAP.get(fmt)
    if not spec:
        continue
    for d in spec["deps"]:
        if d not in lv_deps:
            lv_deps.append(d)
lv["deps"] = lv_deps

# 2b) options: force cplusplus=true and set the format toggles.
opts = lv.get("options", [])

def set_opt(opts, key, value):
    """Replace -Dkey=... (any value) or append if absent."""
    prefix = f"-D{key}="
    for i, o in enumerate(opts):
        if isinstance(o, str) and o.strip().startswith(prefix):
            opts[i] = f"-D{key}={value}"
            return
    opts.append(f"-D{key}={value}")

# sharp needs the C++ binding
set_opt(opts, "cplusplus", "true")

for fmt in formats:
    spec = FORMAT_MAP.get(fmt)
    if not spec:
        continue
    for key, value in spec["options"].items():
        set_opt(opts, key, value)

lv["options"] = opts

with open(LIBVIPS, "w", encoding="utf-8") as f:
    yaml.safe_dump(lv, f, sort_keys=False, allow_unicode=True)
print(f"libvips/lib.yaml: cplusplus=true, formats={formats}, deps={lv_deps}")
