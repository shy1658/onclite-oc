#!/usr/bin/env python3
"""
onclite (Xiaomi Redmi 7 / Redmi Y3, SDM632) CPU/GPU overclock patcher.

Target kernel: Qualcomm CAF msm-4.9 tree used by LineageOS / MIUI.
Files touched:
  arch/arm64/boot/dts/qcom/sdm632.dtsi
  arch/arm64/boot/dts/qcom/sdm632-regulator.dtsi
  drivers/clk/msm/clock-gcc-8953.c

Levels:
  safe   CPU big 2.016 GHz (CPR corner 7 as shipped), GPU 725 MHz
         (SDM632 TURBO LDO table, already in the kernel). Stock voltages.
  high   CPU big 2.208 GHz (APCS PLL max_rate, CPR 7 -> 9 corners,
         ceiling 1.065 V), GPU 800 MHz (new GPLL3 1.60 GHz entry).
  max    same CPU as high but the two new CPR corners use 1.10 V,
         GPU 825 MHz (GPLL3 1.65 GHz) and the GPU LDO TURBO ceiling is
         lifted 750 -> 800 mV.

Legacy names: mild = safe, extreme = high, extreme-v = max.

Why 2.208 GHz and not more: apcs_hf_pll.max_rate is 2208000000 in
clock-cpu-8953.c, so cpufreq rejects anything above it.
Why 825 MHz and not more: GPLL3's VCO table is 1.0-2.0 GHz and the gfx LDO
only exposes TURBO (corner 7) as the top rail, so 1.65 GHz / 825 MHz is the
last stop before having to rewrite the LDO driver.

The patcher keeps a backup in <kernel>/.oc-backup/<timestamp>/ and records
the applied profile in <kernel>/.oc-state.json. Re-running with another
profile restores the backup first. Use --restore to roll back.

This tool edits source only. It does not flash anything.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

DTS = "arch/arm64/boot/dts/qcom/sdm632.dtsi"
REG = "arch/arm64/boot/dts/qcom/sdm632-regulator.dtsi"
CLK = "drivers/clk/msm/clock-gcc-8953.c"
STATE = ".oc-state.json"

C1_TABLES = (
    "qcom,speed0-bin-v0-c1",
    "qcom,speed2-bin-v0-c1",
    "qcom,speed6-bin-v0-c1",
)

# GPU clock table rows: (freq, GPLL3 rate). Both sit inside the alpha PLL's
# 1.0-2.0 GHz VCO window, so they only need a table entry, not a new PLL
# configuration.
GPU_ENTRIES = (
    (800000000, 1600000000),
    (825000000, 1650000000),
)

LEVELS = {
    "safe": {"cpu_khz": 2016000, "gpu_mhz": 725, "cpr_ceiling": None, "gfx_ldo_uv": None},
    "high": {"cpu_khz": 2208000, "gpu_mhz": 800, "cpr_ceiling": 1065000, "gfx_ldo_uv": None},
    "max": {"cpu_khz": 2208000, "gpu_mhz": 825, "cpr_ceiling": 1100000, "gfx_ldo_uv": 800000},
}

ALIASES = {"mild": "safe", "extreme": "high", "extreme-v": "max"}


class PatchError(RuntimeError):
    pass


def fail(msg: str) -> None:
    raise PatchError(msg)


def read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.is_file():
        fail(f"missing file: {rel}")
    return path.read_text(encoding="utf-8")


def write(root: Path, rel: str, text: str) -> None:
    (root / rel).write_text(text, encoding="utf-8")


def prop_pattern(name: str) -> re.Pattern:
    # Matches: name = <..>, <..>, <..>;
    return re.compile(
        rf"({re.escape(name)}\s*=\s*)((?:<[^>]*>\s*,?\s*)+);",
        re.S,
    )


def add_pairs(text: str, name: str, pairs: list[tuple[int, int]]) -> tuple[str, list]:
    match = prop_pattern(name).search(text)
    if not match:
        fail(f"property not found: {name}")
    body = match.group(2)
    found = {(int(a), int(b)) for a, b in re.findall(r"<\s*(\d+)\s+(\d+)\s*>", body)}
    added = [p for p in pairs if p not in found]
    if not added:
        return text, []
    body_new = body.rstrip()
    if not body_new.endswith(","):
        body_new += ","
    body_new += " " + ", ".join(f"<{a} {b}>" for a, b in added)
    return text[: match.start(2)] + body_new + text[match.end(2) :], added


def add_values(text: str, name: str, values: list[int]) -> tuple[str, list]:
    match = prop_pattern(name).search(text)
    if not match:
        fail(f"property not found: {name}")
    body = match.group(2)
    found = {int(v) for v in re.findall(r"<\s*(\d+)\s*>", body)}
    added = [v for v in values if v not in found]
    if not added:
        return text, []
    body_new = body.rstrip()
    if not body_new.endswith(","):
        body_new += ","
    body_new += " " + ", ".join(f"<{v}>" for v in added)
    return text[: match.start(2)] + body_new + text[match.end(2) :], added


def append_array(text: str, name: str, values: list[int]) -> tuple[str, list]:
    match = prop_pattern(name).search(text)
    if not match:
        fail(f"property not found: {name}")
    body = match.group(2)
    inner = re.search(r"<([^>]*)>", body, re.S)
    if not inner:
        fail(f"malformed array: {name}")
    old = inner.group(1).rstrip()
    new = old + " " + " ".join(str(v) for v in values)
    body_new = body[: inner.start(1)] + new + body[inner.end(1) :]
    return text[: match.start(2)] + body_new + text[match.end(2) :], values


def replace_inner(text: str, name: str, new_inner: str) -> str:
    match = prop_pattern(name).search(text)
    if not match:
        fail(f"property not found: {name}")
    body = match.group(2)
    inner = re.search(r"<([^>]*)>", body, re.S)
    if not inner:
        fail(f"malformed array: {name}")
    body_new = body[: inner.start(1)] + new_inner + body[inner.end(1) :]
    return text[: match.start(2)] + body_new + text[match.end(2) :]


def replace_scalar(text: str, name: str, old: str, new: str) -> str:
    pattern = re.compile(rf"({re.escape(name)}\s*=\s*<){re.escape(old)}(>;)")
    if not pattern.search(text):
        fail(f"scalar not found: {name} = <{old}>")
    return pattern.sub(rf"\g<1>{new}\g<2>", text, count=1)


def set_last_values(text: str, name: str, values: list[int]) -> str:
    match = prop_pattern(name).search(text)
    if not match:
        fail(f"property not found: {name}")
    body = match.group(2)
    inner = re.search(r"<([^>]*)>", body, re.S)
    if not inner:
        fail(f"malformed array: {name}")
    parts = inner.group(1).split()
    if len(parts) < len(values):
        fail(f"array too short: {name}")
    parts[-len(values) :] = [str(v) for v in values]
    body_new = body[: inner.start(1)] + " ".join(parts) + body[inner.end(1) :]
    return text[: match.start(2)] + body_new + text[match.end(2) :]


def node_block(text: str, label: str) -> tuple[int, int]:
    start = text.find(label)
    if start < 0:
        fail(f"node not found: {label}")
    brace = text.find("{", start)
    if brace < 0:
        fail(f"node has no body: {label}")
    depth = 0
    for i in range(brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return start, i + 1
    fail(f"unbalanced braces in node: {label}")


# --------------------------------------------------------------------------
# CPU
# --------------------------------------------------------------------------


def patch_cpu(text: str, cpu_khz: int) -> tuple[str, list[str]]:
    changes: list[str] = []
    pairs = [(2016000000, 7)]
    if cpu_khz >= 2208000:
        pairs += [(2150400000, 8), (2208000000, 9)]

    for prop in C1_TABLES:
        text, added = add_pairs(text, prop, pairs)
        if added:
            changes.append(f"{prop}: +{added}")

    if cpu_khz >= 2208000:
        text, added = add_values(text, "qcom,cpufreq-table-4", [2150400, 2208000])
        if added:
            changes.append(f"qcom,cpufreq-table-4: +{added}")

    return text, changes


def patch_cpr(text: str, cpu_khz: int, ceiling_uv: int | None) -> tuple[str, list[str]]:
    if cpu_khz < 2208000:
        return text, []

    start, end = node_block(text, "apc1_perfcl_vreg")
    block = text[start:end]
    changes: list[str] = []

    block = replace_scalar(block, "regulator-max-microvolt", "7", "9")
    block = replace_scalar(block, "qcom,cpr-corners", "7", "9")
    block = replace_inner(block, "qcom,cpr-corner-fmax-map", "1 3 4 9")

    for name, values in (
        ("qcom,cpr-voltage-ceiling", [1065000, 1065000]),
        ("qcom,cpr-voltage-floor", [500000, 500000]),
        ("qcom,cpr-floor-to-ceiling-max-range", [50000, 50000]),
        ("qcom,mem-acc-voltage", [3, 3]),
        ("qcom,corner-frequencies", [2150400000, 2208000000]),
    ):
        block, _ = append_array(block, name, values)

    if ceiling_uv and ceiling_uv > 1065000:
        block = set_last_values(block, "qcom,cpr-voltage-ceiling", [ceiling_uv, ceiling_uv])

    top = f", ceiling {ceiling_uv // 1000} mV" if ceiling_uv else ""
    changes.append(f"apc1_perfcl_vreg: 7 -> 9 corners{top}")
    return text[:start] + block + text[end:], changes


# --------------------------------------------------------------------------
# GPU
# --------------------------------------------------------------------------


def patch_gfx_clock(text: str, gpu_mhz: int) -> tuple[str, list[str]]:
    """Point gfx3d_clk_src at the SDM632 table and add the OC rows."""
    if "ftbl_gfx3d_clk_src_sdm632" not in text:
        fail("clock-gcc-8953.c has no ftbl_gfx3d_clk_src_sdm632 table")

    changes: list[str] = []
    old = ".freq_tbl = ftbl_gfx3d_clk_src,"
    new = ".freq_tbl = ftbl_gfx3d_clk_src_sdm632,"
    if new not in text:
        if text.count(old) != 1:
            fail(f"expected exactly one '{old}', found {text.count(old)}")
        text = text.replace(old, new, 1)
        changes.append("gfx3d_clk_src -> SDM632 table")

    anchor = "static struct clk_freq_tbl ftbl_gfx3d_clk_src_sdm632[] = {"
    start = text.find(anchor)
    if start < 0:
        fail("gfx3d SDM632 table header not found")
    stop = text.find("\tF_END", start)
    if stop < 0:
        fail("gfx3d SDM632 table has no F_END")
    block = text[start:stop]

    rows = []
    for freq, src in GPU_ENTRIES:
        if freq > gpu_mhz * 1000000:
            continue
        if re.search(rf"F_MM\(\s*{freq}\s*,", block):
            continue
        rows.append(f"\tF_MM( {freq},    {src},               gpll3,    1,    0,     0),\n")
    if rows:
        text = text[:stop] + "".join(rows) + text[stop:]
        added = ", ".join(f"{freq // 1000000} MHz" for freq, _ in GPU_ENTRIES
                          if freq <= gpu_mhz * 1000000)
        changes.append(f"gfx3d table: +{added}")
    return text, changes


def patch_gfx_corner(text: str, gpu_mhz: int) -> tuple[str, list[str]]:
    """Map the new GPU steps to the TURBO LDO corner (7) in gfxfreq-corner."""
    pattern = re.compile(r"<\s*725000000\s+7\s*>\s*;")
    match = pattern.search(text)
    if not match:
        fail("qcom,gfxfreq-corner has no '< 725000000 7 >' entry")

    wanted = [freq for freq, _ in GPU_ENTRIES if freq <= gpu_mhz * 1000000]
    tail = text[match.end() : match.end() + 300]
    rows = [freq for freq in wanted if str(freq) not in tail]
    if not rows:
        return text, []

    replacement = (
        "< 725000000   7 >,  /* Turbo */\n"
        + "".join(f"\t\t < {freq}   7 >,\n" for freq in rows[:-1])
        + f"\t\t < {rows[-1]}   7 >;"
    )
    new = text[: match.start()] + replacement + text[match.end() :]
    return new, ["gfxfreq-corner: +" + ", ".join(f"{f // 1000000} MHz" for f in rows)]


def patch_pwrlevels(text: str, gpu_mhz: int) -> tuple[str, list[str]]:
    """Insert a new top GPU power level and shift the rest down by one."""
    if gpu_mhz <= 725:
        return text, []

    start, end = node_block(text, "qcom,gpu-pwrlevels {")
    block = text[start:end]
    if f"qcom,gpu-freq = <{gpu_mhz * 1000000}>;" in block:
        return text, []

    marker = 'compatible = "qcom,gpu-pwrlevels";'
    pos = block.find(marker)
    if pos < 0:
        fail("gpu-pwrlevels has no compatible line")

    # Raise every existing index by one first, then drop the new node in at 0.
    # Both patterns are monotonic so there is no collision.
    block = re.sub(
        r"qcom,gpu-pwrlevel@(\d+)",
        lambda m: f"qcom,gpu-pwrlevel@{int(m.group(1)) + 1}",
        block,
    )
    block = re.sub(
        r"reg = <(\d+)>;",
        lambda m: f"reg = <{int(m.group(1)) + 1}>;",
        block,
    )

    node = (
        f"\n\t\t/* TURBO LDO OC */\n"
        f"\t\tqcom,gpu-pwrlevel@0 {{\n"
        f"\t\t\treg = <0>;\n"
        f"\t\t\tqcom,gpu-freq = <{gpu_mhz * 1000000}>;\n"
        f"\t\t\tqcom,bus-freq = <10>;\n"
        f"\t\t\tqcom,bus-min = <10>;\n"
        f"\t\t\tqcom,bus-max = <10>;\n"
        f"\t\t}};\n"
    )
    block = block[: pos + len(marker)] + node + block[pos + len(marker) :]

    text = text[:start] + block + text[end:]

    # ca-target / initial levels move with the list so boot behaviour stays
    # identical (320 MHz initial, 400 MHz CA target).
    text = re.sub(
        r"(qcom,(?:ca-target|initial)-pwrlevel\s*=\s*<)(\d+)(>;)",
        lambda m: f"{m.group(1)}{int(m.group(2)) + 1}{m.group(3)}",
        text,
        count=2,
    )
    return text, [f"gpu-pwrlevels: +{gpu_mhz} MHz at level 0 (10 levels)"]


def patch_gfx_ldo(text: str, ceiling_uv: int | None) -> tuple[str, list[str]]:
    if not ceiling_uv:
        return text, []
    pattern = re.compile(r"(qcom,ldo-voltage-ceiling\s*=\s*<\s*\d+\s+\d+\s+)(\d+)(\s*>;)")
    match = pattern.search(text)
    if not match:
        fail("qcom,ldo-voltage-ceiling not found")
    old = int(match.group(2))
    if old >= ceiling_uv:
        return text, []
    new = text[: match.start(2)] + str(ceiling_uv) + text[match.end(2) :]
    return new, [f"gfx LDO TURBO ceiling {old // 1000} -> {ceiling_uv // 1000} mV"]


# --------------------------------------------------------------------------
# validate / state
# --------------------------------------------------------------------------


def validate(dts: str, reg: str, clk: str, level: dict) -> None:
    if "ftbl_gfx3d_clk_src_sdm632" not in clk:
        fail("GPU table selection was not applied")

    gpu_mhz = level["gpu_mhz"]
    for freq, _ in GPU_ENTRIES:
        if freq <= gpu_mhz * 1000000 and not re.search(rf"F_MM\(\s*{freq}\s*,", clk):
            fail(f"GPU {freq // 1000000} MHz row missing from the clock table")

    start, end = node_block(dts, "qcom,gpu-pwrlevels {")
    block = dts[start:end]
    top = re.search(
        r"qcom,gpu-pwrlevel@0\s*\{\s*reg = <0>;\s*qcom,gpu-freq = <(\d+)>;", block, re.S
    )
    if not top:
        fail("gpu-pwrlevel@0 missing")
    if int(top.group(1)) != gpu_mhz * 1000000:
        fail(f"gpu-pwrlevel@0 is {top.group(1)}, expected {gpu_mhz * 1000000}")

    corner = re.search(r"qcom,gfxfreq-corner\s*=(.*?);", dts, re.S)
    if not corner or f"{gpu_mhz * 1000000}" not in corner.group(1):
        fail(f"gfxfreq-corner has no {gpu_mhz} MHz entry")

    if level["cpu_khz"] >= 2208000:
        for prop in C1_TABLES:
            match = prop_pattern(prop).search(dts)
            if not match or 2208000000 not in {
                int(v) for v in re.findall(r"<\s*(\d+)\s+", match.group(2))
            }:
                fail(f"{prop} does not contain 2208000000")
        start, end = node_block(reg, "apc1_perfcl_vreg")
        block = reg[start:end]
        if "qcom,cpr-corners = <9>;" not in block:
            fail("CPR corners were not expanded")
        ceiling = prop_pattern("qcom,cpr-voltage-ceiling").search(block)
        if not ceiling or len(re.findall(r"\d+", ceiling.group(2))) != 9:
            fail("CPR ceiling array is not 9 entries")


def profile_key(level: dict) -> dict:
    return {k: level[k] for k in ("cpu_khz", "gpu_mhz", "cpr_ceiling", "gfx_ldo_uv")}


def backup(root: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = root / ".oc-backup" / stamp
    for rel in (DTS, REG, CLK):
        src = root / rel
        if src.is_file():
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
    return dst


def restore(root: Path, state: dict) -> None:
    backup_dir = state.get("backup")
    if not backup_dir:
        fail("no backup recorded in .oc-state.json")
    src_root = root / backup_dir
    if not src_root.is_dir():
        fail(f"backup directory missing: {backup_dir}")
    for rel in (DTS, REG, CLK):
        src = src_root / rel
        if src.is_file():
            shutil.copy2(src, root / rel)
    (root / STATE).unlink(missing_ok=True)
    print(f"restored from {backup_dir}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch onclite msm-4.9 for CPU/GPU overclock")
    parser.add_argument("kernel_dir", help="path to the kernel source tree")
    parser.add_argument(
        "--level",
        choices=("safe", "high", "max", "mild", "extreme", "extreme-v"),
        default="safe",
    )
    parser.add_argument("--cpu", type=int, choices=(2016, 2208), help="override CPU ceiling (MHz)")
    parser.add_argument("--gpu", type=int, choices=(725, 800, 825), help="override GPU ceiling (MHz)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    root = Path(args.kernel_dir).resolve()
    if not (root / "arch" / "arm64").is_dir():
        fail(f"not a kernel tree: {root}")

    level_name = ALIASES.get(args.level, args.level)
    level = dict(LEVELS[level_name])
    if args.cpu:
        level["cpu_khz"] = args.cpu * 1000
        if level["cpu_khz"] < 2208000:
            level["cpr_ceiling"] = None
        elif level["cpr_ceiling"] is None:
            level["cpr_ceiling"] = 1065000
    if args.gpu:
        level["gpu_mhz"] = args.gpu
        if level["gpu_mhz"] < 825:
            level["gfx_ldo_uv"] = None

    state_path = root / STATE
    state: dict = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}

    if args.restore:
        restore(root, state)
        return 0

    current = state.get("profile") or {}
    wanted = profile_key(level)
    if state and current == wanted and not args.force:
        print(f"already patched: {args.level} (use --force to reapply)")
        return 0
    if state:
        if current == wanted:
            print(f"re-applying {args.level}, restoring first")
        else:
            print(f"switching {state.get('level', '?')} -> {args.level}, restoring first")
        restore(root, state)

    dts = read(root, DTS)
    reg = read(root, REG)
    clk = read(root, CLK)

    dts, cpu_changes = patch_cpu(dts, level["cpu_khz"])
    reg, cpr_changes = patch_cpr(reg, level["cpu_khz"], level["cpr_ceiling"])
    clk, gfx_clock_changes = patch_gfx_clock(clk, level["gpu_mhz"])
    dts, corner_changes = patch_gfx_corner(dts, level["gpu_mhz"])
    dts, pwrlevel_changes = patch_pwrlevels(dts, level["gpu_mhz"])
    reg, ldo_changes = patch_gfx_ldo(reg, level["gfx_ldo_uv"])

    changes = (
        cpu_changes
        + cpr_changes
        + gfx_clock_changes
        + corner_changes
        + pwrlevel_changes
        + ldo_changes
    )
    validate(dts, reg, clk, level)

    if args.dry_run:
        print(f"dry run: {args.level} (cpu {level['cpu_khz'] // 1000} MHz, gpu {level['gpu_mhz']} MHz)")
        for change in changes:
            print(f"  {change}")
        return 0

    backup_dir = backup(root)
    write(root, DTS, dts)
    write(root, REG, reg)
    write(root, CLK, clk)
    state_path.write_text(
        json.dumps(
            {
                "level": args.level,
                "profile": wanted,
                "backup": str(backup_dir.relative_to(root)).replace("\\", "/"),
                "applied": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"applied: {args.level} (cpu {level['cpu_khz'] // 1000} MHz, gpu {level['gpu_mhz']} MHz)")
    for change in changes:
        print(f"  {change}")
    print(f"backup: {backup_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
