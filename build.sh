#!/usr/bin/env bash
# Build an overclocked onclite (Redmi 7 / Y3, SDM632) kernel and package it
# as an AnyKernel3 zip. Run inside WSL2/Ubuntu, not Git Bash.
#
#   LEVEL=safe   ./build.sh
#   LEVEL=max    ./build.sh
#
# Env:
#   LEVEL   safe | high | max                      (default: safe)
#           safe = 2.016 GHz + GPU 725, stock volts
#           high = 2.208 GHz (1.065 V) + GPU 800
#           max  = 2.208 GHz (1.10 V)  + GPU 825 + LDO 800 mV
#   GPU     725 | 800 | 825   optional override of LEVEL's GPU ceiling
#   CPU     2016 | 2208       optional override of LEVEL's CPU ceiling
#   BRANCH  kernel branch                          (default: lineage-21)
#   REPO    kernel repo                            (default: LineageOS)
#   WORK    build directory                        (default: ./work)
#   JOBS    parallel jobs                          (default: nproc)
#   BOOT_IMG  stock boot.img or LineageOS zip; when set, the script
#             repacks it into boot-onclite-oc-<level>.img
set -euo pipefail

LEVEL="${LEVEL:-safe}"
BRANCH="${BRANCH:-lineage-21}"
REPO="${REPO:-https://github.com/LineageOS/android_kernel_xiaomi_onclite.git}"
WORK="${WORK:-$PWD/work}"
JOBS="${JOBS:-$(nproc)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
    Linux) ;;
    *) die "run this in WSL2 or a Linux VM (make/gcc are required)" ;;
esac

for tool in git make python3 bc bison flex zip; do
    command -v "$tool" >/dev/null 2>&1 || die "missing tool: $tool (apt install build-essential bc bison flex libssl-dev libelf-dev zip)"
done

mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"

if [ ! -d "$WORK/kernel/.git" ]; then
    say "cloning $REPO ($BRANCH)"
    git clone --depth 1 --single-branch -b "$BRANCH" "$REPO" "$WORK/kernel"
else
    say "reusing $WORK/kernel"
fi

say "applying overclock patches: $LEVEL"
PATCH_ARGS=(--level "$LEVEL")
if [ -n "${GPU:-}" ]; then PATCH_ARGS+=(--gpu "$GPU"); fi
if [ -n "${CPU:-}" ]; then PATCH_ARGS+=(--cpu "$CPU"); fi
python3 "$SCRIPT_DIR/apply_oc.py" "$WORK/kernel" "${PATCH_ARGS[@]}"

say "building Image.gz-dtb"
cd "$WORK/kernel"

if command -v aarch64-linux-gnu-gcc >/dev/null 2>&1; then
    TOOLCHAIN=gcc
    MAKE_ARGS=(ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu-)
else
    TOOLCHAIN=clang
    if [ ! -d "$WORK/proton-clang" ]; then
        say "aarch64-linux-gnu-gcc not found, fetching proton-clang"
        git clone --depth 1 https://github.com/kdrag0n/proton-clang.git "$WORK/proton-clang"
    fi
    export PATH="$WORK/proton-clang/bin:$PATH"
    MAKE_ARGS=(ARCH=arm64 CC=clang CLANG_TRIPLE=aarch64-linux-gnu- CROSS_COMPILE=aarch64-linux-gnu-)
fi

say "toolchain: $TOOLCHAIN"
make O=out "${MAKE_ARGS[@]}" onclite-perf_defconfig
make O=out "${MAKE_ARGS[@]}" -j"$JOBS" Image.gz-dtb

IMAGE="$WORK/kernel/out/arch/arm64/boot/Image.gz-dtb"
[ -f "$IMAGE" ] || die "build finished but $IMAGE is missing"

if [ -n "${BOOT_IMG:-}" ]; then
    say "repacking boot image"
    OUT_IMG="$WORK/boot-onclite-oc-${LEVEL}.img"
    python3 "$SCRIPT_DIR/repack_boot.py" "$BOOT_IMG" "$IMAGE" "$OUT_IMG"

    say "done"
    printf 'image: %s\n' "$OUT_IMG"
    printf 'next : patch this img with Kitsune Mask, then flash it to boot in recovery\n'
    printf 'rollback: reflash the stock boot.img you backed up before testing\n'
else
    say "BOOT_IMG not set, packaging AnyKernel3 zip instead"
    if [ ! -d "$WORK/AnyKernel3/.git" ]; then
        git clone --depth 1 https://github.com/osm0sis/AnyKernel3.git "$WORK/AnyKernel3"
    fi

    cp "$SCRIPT_DIR/anykernel.sh" "$WORK/AnyKernel3/anykernel.sh"
    cp "$IMAGE" "$WORK/AnyKernel3/Image.gz-dtb"

    OUT="$WORK/onclite-oc-${LEVEL}.zip"
    rm -f "$OUT"
    (
        cd "$WORK/AnyKernel3"
        zip -r9 "$OUT" . -x ".git/*" "README.md" "*.zip"
    )

    say "done"
    printf 'zip: %s\n' "$OUT"
    printf 'for a raw boot.img, set BOOT_IMG=/path/to/boot.img and re-run\n'
fi
