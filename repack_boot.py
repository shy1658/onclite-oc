#!/usr/bin/env python3
"""
Replace the kernel inside an Android boot image (header v0/v1/v2) and keep
the original ramdisk, cmdline and addresses intact. This is what you want
before patching the image with Kitsune Mask / Magisk.

Usage:
  python repack_boot.py <stock-boot.img|lineage-*.zip> <Image.gz-dtb> <out.img>

The input may be a raw boot.img or a LineageOS install zip containing boot.img.
Only the kernel blob is replaced; the ramdisk is copied byte-for-byte, so
Kitsune Mask can patch it afterwards.
"""

from __future__ import annotations

import hashlib
import io
import struct
import sys
import zipfile
from pathlib import Path

MAGIC = b"ANDROID!"
HEADER_V0 = 1632
HEADER_V1 = 1648
HEADER_V2 = 1660


class BootError(RuntimeError):
    pass


def fail(msg: str) -> None:
    raise BootError(msg)


def align(value: int, page: int) -> int:
    return (value + page - 1) // page * page


def load_image(path: Path) -> bytes:
    data = path.read_bytes()
    if data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            for name in names:
                base = name.rsplit("/", 1)[-1].lower()
                if base == "boot.img":
                    return archive.read(name)
        fail(f"no boot.img found inside {path.name}")
    return data


def parse(data: bytes) -> dict:
    if len(data) < HEADER_V0 or data[:8] != MAGIC:
        fail("not an Android boot image (magic mismatch)")

    values = struct.unpack_from("<10I", data, 8)
    (
        kernel_size,
        kernel_addr,
        ramdisk_size,
        ramdisk_addr,
        second_size,
        second_addr,
        tags_addr,
        page_size,
        header_version,
        os_version,
    ) = values

    if page_size not in (2048, 4096, 8192, 16384):
        fail(f"unexpected page size: {page_size}")
    if header_version not in (0, 1, 2):
        fail(f"unsupported boot header version: {header_version}")

    if header_version >= 1:
        if len(data) < HEADER_V1:
            fail("truncated v1 header")
        recovery_dtbo_size, recovery_dtbo_offset, header_size = struct.unpack_from(
            "<IQI", data, 1632
        )
    else:
        recovery_dtbo_size = 0
        recovery_dtbo_offset = 0
        header_size = HEADER_V0

    if header_version >= 2:
        if len(data) < HEADER_V2:
            fail("truncated v2 header")
        dtb_size, dtb_addr = struct.unpack_from("<IQ", data, 1648)
    else:
        dtb_size = 0
        dtb_addr = 0

    kernel_off = page_size
    ramdisk_off = align(kernel_off + kernel_size, page_size)
    second_off = align(ramdisk_off + ramdisk_size, page_size)
    recovery_off = (
        recovery_dtbo_offset
        if recovery_dtbo_offset
        else align(second_off + second_size, page_size)
    )
    dtb_off = align(recovery_off + recovery_dtbo_size, page_size)

    def take(offset: int, size: int, label: str) -> bytes:
        if size == 0:
            return b""
        if offset + size > len(data):
            fail(f"{label} extends past the end of the image")
        return data[offset : offset + size]

    return {
        "header_version": header_version,
        "page_size": page_size,
        "kernel_size": kernel_size,
        "kernel_addr": kernel_addr,
        "ramdisk_size": ramdisk_size,
        "ramdisk_addr": ramdisk_addr,
        "second_size": second_size,
        "second_addr": second_addr,
        "tags_addr": tags_addr,
        "os_version": os_version,
        "name": data[48:64],
        "cmdline": data[64:576],
        "extra_cmdline": data[608:1632],
        "header_size": header_size or (HEADER_V1 if header_version == 1 else HEADER_V2),
        "recovery_dtbo_size": recovery_dtbo_size,
        "dtb_size": dtb_size,
        "dtb_addr": dtb_addr,
        "kernel": take(kernel_off, kernel_size, "kernel"),
        "ramdisk": take(ramdisk_off, ramdisk_size, "ramdisk"),
        "second": take(second_off, second_size, "second"),
        "recovery_dtbo": take(recovery_off, recovery_dtbo_size, "recovery_dtbo"),
        "dtb": take(dtb_off, dtb_size, "dtb"),
    }


def build(image: dict, new_kernel: bytes) -> bytes:
    page = image["page_size"]
    version = image["header_version"]
    header_size = image["header_size"]

    kernel = new_kernel
    ramdisk = image["ramdisk"]
    second = image["second"]
    recovery = image["recovery_dtbo"]
    dtb = image["dtb"]

    kernel_off = page
    ramdisk_off = align(kernel_off + len(kernel), page)
    second_off = align(ramdisk_off + len(ramdisk), page)
    recovery_off = align(second_off + len(second), page)
    dtb_off = align(recovery_off + len(recovery), page)

    header = bytearray(header_size)
    struct.pack_into(
        "<8s10I",
        header,
        0,
        MAGIC,
        len(kernel),
        image["kernel_addr"],
        len(ramdisk),
        image["ramdisk_addr"],
        len(second),
        image["second_addr"],
        image["tags_addr"],
        page,
        version,
        image["os_version"],
    )
    header[48:64] = image["name"]
    header[64:576] = image["cmdline"]
    header[608:1632] = image["extra_cmdline"]

    if version >= 1:
        struct.pack_into("<IQI", header, 1632, len(recovery), recovery_off, header_size)
    if version >= 2:
        struct.pack_into("<IQ", header, 1648, len(dtb), image["dtb_addr"])

    digest = hashlib.sha1()
    digest.update(kernel)
    digest.update(struct.pack("<I", len(kernel)))
    digest.update(ramdisk)
    digest.update(struct.pack("<I", len(ramdisk)))
    digest.update(second)
    digest.update(struct.pack("<I", len(second)))
    header[576:596] = digest.digest()
    header[596:608] = b"\x00" * 12

    output = bytearray()
    output += bytes(header).ljust(page, b"\x00")
    output += kernel.ljust(align(len(kernel), page), b"\x00")
    output += ramdisk.ljust(align(len(ramdisk), page), b"\x00")
    output += second.ljust(align(len(second), page), b"\x00")
    if version >= 1:
        output += recovery.ljust(align(len(recovery), page), b"\x00")
    if version >= 2:
        output += dtb.ljust(align(len(dtb), page), b"\x00")
    return bytes(output)


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: python repack_boot.py <stock-boot.img|rom.zip> <Image.gz-dtb> <out.img>",
            file=sys.stderr,
        )
        return 2

    stock_path = Path(argv[1])
    kernel_path = Path(argv[2])
    out_path = Path(argv[3])

    if not stock_path.is_file():
        fail(f"missing input: {stock_path}")
    if not kernel_path.is_file():
        fail(f"missing kernel: {kernel_path}")

    new_kernel = kernel_path.read_bytes()
    if not new_kernel:
        fail("kernel image is empty")
    if not new_kernel.startswith(b"\x1f\x8b"):
        print("warning: kernel is not gzip-compressed; expected Image.gz-dtb", file=sys.stderr)

    image = parse(load_image(stock_path))
    output = build(image, new_kernel)
    out_path.write_bytes(output)

    print(f"stock  : v{image['header_version']} page={image['page_size']} "
          f"kernel={len(image['kernel'])} ramdisk={len(image['ramdisk'])}")
    print(f"new    : kernel={len(new_kernel)} total={len(output)}")
    print(f"written: {out_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except BootError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
