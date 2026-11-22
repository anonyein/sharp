#!/usr/bin/env python3
"""Patch sharp's src/binding.gyp so it can be cross-compiled for Android.

sharp is a Node-API addon that consumes libvips' C++ API. For Android we:

  * force the `use_global_libvips == "true"` branch (driven by
    SHARP_FORCE_GLOBAL_LIBVIPS=1 in the workflow), so include/lib flags come
    from pkg-config against the libvips we staged for the target ABI;
  * neutralise the Linux-only `_GLIBCXX_USE_CXX11_ABI` probe, which shells out
    to `readelf` on a host-native libvips-cpp.so and is meaningless for an NDK
    libc++ cross build;
  * link the Javet Node runtime .so in the global-libvips branch to resolve
    napi_* at link time, alongside libvips-cpp;
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

# 2) Keep the pkg-config library flags and append Javet in the same branch.
src, n_link = re.subn(
    r"^([ \t]*)'libraries': \[('<!@\([^\n]*pkg-config --libs vips-cpp\)')\],$",
    r"\1# SHARP_ANDROID_PATCH: resolve napi_* against the Javet runtime\n"
    r"""\1'libraries': [\2, '<!(echo "$IV_JAVET_LIB")'],""" "\n"
    r"\1'ldflags': ['-Wl,--no-undefined'],",
    src,
    flags=re.MULTILINE,
)
if n_link != 1:
    sys.exit("expected one global libvips-cpp pkg-config link entry")

# 3) No rpath needed on Android. Android's dynamic linker resolves DT_NEEDED
#    dependencies from the app's nativeLibraryDir (the unpacked jniLibs/<abi>/),
#    where sharp.node and all the libvips/glib .so files live side by side. This
#    is unlike desktop Linux, which needs rpath $ORIGIN to find sibling libs.
#    So we deliberately omit rpath to keep the binary lean.

with open(GYP, "w", encoding="utf-8") as f:
    f.write(src)

print(
    f"patched sharp binding.gyp (probe replaced: {n_probe}, "
    f"-std raised to c++17: {n_std}, Javet link entry added: {n_link})"
)
