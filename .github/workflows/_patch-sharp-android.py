#!/usr/bin/env python3
"""Patch sharp's src/binding.gyp so it can be cross-compiled for Android.

sharp is a Node-API addon that consumes libvips' C++ API. For Android we:

  * force the `use_global_libvips == "true"` branch (driven by
    SHARP_FORCE_GLOBAL_LIBVIPS=1 in the workflow), so include/lib flags come
    from pkg-config against the libvips we staged for the target ABI;
  * neutralise the Linux-only `_GLIBCXX_USE_CXX11_ABI` probe, which shells out
    to `readelf` on a host-native libvips-cpp.so and is meaningless for an NDK
    libc++ cross build;
  * add an `OS == "linux"` link block (gyp's OS is "linux" when cross-compiling
    on a Linux host) that links the Javet Node runtime .so to resolve napi_*
    at link time, alongside libvips-cpp;
  * raise sharp's own `-std=c++0x` (i.e. C++11) to C++17. sharp's target-level
    cflags_cc are appended *after* node's common.gypi `-std=gnu++20`, and the
    last -std on the command line wins, so upstream effectively pins the addon
    to C++11. node-addon-api 8.9.2 (2026-08-12) started using std::void_t,
    std::enable_if_t, std::is_null_pointer_v, std::decay_t and `if constexpr`
    in napi.h, none of which exist in C++11, so the build breaks with
    "no template named 'void_t' in namespace 'std'". sharp declares
    node-addon-api "^8.1.0", so this lands automatically on fresh installs.

No rpath is needed: Android's linker resolves every DT_NEEDED from the app's
native library dir (jniLibs/<abi>/), where sharp.node and all the libvips/glib
.so files live side by side.

The Javet .so absolute path is taken from env IV_JAVET_LIB.

Idempotent: re-running detects the SHARP_ANDROID_PATCH marker and no-ops.

Args:
  argv[1]  path to sharp's src/binding.gyp
Env:
  IV_ABI        Android ABI (informational)
  IV_JAVET_SO   basename of the Javet .so (informational)
  IV_JAVET_LIB  absolute path to the Javet .so to link (required at build time,
                but the patch embeds a <(module_root_dir)-independent abs path
                via the IV_JAVET_LIB gyp variable expansion below)
"""

import os
import re
import sys

if len(sys.argv) < 2:
    sys.exit("usage: _patch-sharp-android.py <path-to-binding.gyp>")

GYP = sys.argv[1]
if not os.path.isfile(GYP):
    sys.exit(f"binding.gyp not found: {GYP}")

with open(GYP, "r", encoding="utf-8") as f:
    src = f.read()

if "SHARP_ANDROID_PATCH" in src:
    print("binding.gyp already patched; skipping")
    sys.exit(0)

# 1) Neutralise the host-only _GLIBCXX_USE_CXX11_ABI readelf probe. It appears
#    inside the use_global_libvips branch's OS=="linux" defines. Replace the
#    dynamic shell-out with a fixed value (NDK libc++ does not use the libstdc++
#    __cxx11 ABI, so 0 is correct and avoids the readelf call entirely).
probe_re = re.compile(
    r"'_GLIBCXX_USE_CXX11_ABI=<!\(if readelf[^\n]*\)'",
)
src, n_probe = probe_re.subn("'_GLIBCXX_USE_CXX11_ABI=0'", src)

# 1b) Raise sharp's own -std=c++0x to C++17.
#
#     gyp emits target cflags_cc after common.gypi's, and clang honours the last
#     -std, so upstream's '-std=c++0x' downgrades the whole addon to C++11.
#     node-addon-api >= 8.9.2 requires C++17 (std::void_t, std::enable_if_t,
#     std::is_null_pointer_v, std::decay_t, `if constexpr`).
std_re = re.compile(r"'-std=c\+\+0x'")
src, n_std = std_re.subn("'-std=c++17'", src)
if n_std == 0:
    # Upstream may have already moved to a newer standard; only fail if there is
    # no acceptable -std at all in the target cflags_cc.
    if not re.search(r"'-std=(c|gnu)\+\+(1[4-9]|2[0-9])'", src):
        sys.exit("could not find sharp's -std=c++0x to raise to C++17")
    print("sharp already targets C++14 or newer; leaving -std alone")

# 2) Inject the Javet Node runtime .so into the link libraries so napi_*
#    symbols resolve at link time (verified via --no-undefined). We keep the
#    existing libvips-cpp link entry and append the Javet .so by absolute path.
#
#    Anchor on the global-libvips linux link libraries entry.
anchor = "'-l:libvips-cpp.so.42'"
if anchor not in src:
    # Some versions use .so without version; try a looser anchor.
    m = re.search(r"'-l:libvips-cpp\.so(\.\d+)?'", src)
    if not m:
        sys.exit("could not find libvips-cpp link entry to anchor Android patch")
    anchor = m.group(0)

javet_lib = os.environ.get("IV_JAVET_LIB", "").strip()
# The link entry becomes: keep libvips-cpp, add the Javet .so by absolute path.
# gyp reads $(IV_JAVET_LIB) from the environment at configure time.
new_libs = (
    "'-l:libvips-cpp.so.42',\n"
    "                # SHARP_ANDROID_PATCH: resolve napi_* against the Javet Node runtime .so\n"
    "                '<!(echo $IV_JAVET_LIB)'"
)
src = src.replace(anchor, new_libs, 1)

# 3) No rpath needed on Android. Android's dynamic linker resolves DT_NEEDED
#    dependencies from the app's nativeLibraryDir (the unpacked jniLibs/<abi>/),
#    where sharp.node and all the libvips/glib .so files live side by side. This
#    is unlike desktop Linux, which needs rpath $ORIGIN to find sibling libs.
#    So we deliberately omit rpath to keep the binary lean.

with open(GYP, "w", encoding="utf-8") as f:
    f.write(src)

print(
    f"patched sharp binding.gyp (probe replaced: {n_probe}, "
    f"-std raised to c++17: {n_std}); linked Javet: {javet_lib}"
)
