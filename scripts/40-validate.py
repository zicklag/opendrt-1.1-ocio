#!/usr/bin/env python3
"""
Validate the OpenDRT 1.1 -> Blender 5.2 config with actual evidence.

Every check prints a PASS/FAIL line with its numbers; the script exits 1 on
any FAIL. Checks, in order:

  gates - bake fidelity, against independently produced cubes
    [gate v1.1]   our 64pt v1.1.0 Standard -> rec1886 bake vs gripeyes'
                  independent bake of the same preset from the same release
                  DCTL - HARD gate at 1e-4.
    [gate v1.0]   our 48pt v1.0.0 bakes vs Jed's own shipped v1.0.0 config
                  cubes - SOFT gate at 6e-2, catching gross errors only:
                  his shipped cubes sit slightly away from the v1.0.0 DCTL
                  in chromatic mids (established empirically in-session).
    [gate spi1d]  the ociogen-generated T-LogE transfer vs Jed's shipped
                  spi1d - HARD gate at 1e-4.

  config - the spliced drop-in config, through the real OCIO library
    [config]  loads; all 35 OpenDRT view colorspaces present and wired into
              the five covered display families; roles intact (checked
              through the OCIO API, falling back to config text); every
              scene_linear -> view colorspace pair builds a processor.
    [white]   ACES reference white maps to E-Gamut2 white through the spliced
              BFD-legend; greys invariant.
    [spot]    greys + primaries through the FULL config pipeline (scene ->
              T-LogE -> cube) vs the same points fed straight to the bake
              exe - the wiring proof. Grey axis must stay neutral through
              every display encoding.

  images
    [images]  check-image PNGs (exposure ramp + primaries + saturation
              sweep) through several views, in
              dist/OpenDRT-1.1-Blender5.2-checks/, for eyeball review.

Usage:  pixi run python scripts/40-validate.py    (after 30-build-config.py)
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("bake_luts", ROOT / "scripts/20-bake-luts.py")
_bake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bake)

DIST = (ROOT / "dist/OpenDRT-1.1-Blender5.2").resolve()
CHECKS = ROOT / "dist/OpenDRT-1.1-Blender5.2-checks"

# D65 white, Y-normalized (same constant the build script uses).
D65_WHITE = np.array([0.3127 / 0.329, 1.0, (1 - 0.3127 - 0.329) / 0.329])

failures = 0


def report(name: str, ok: bool, detail: str) -> None:
    global failures
    if not ok:
        failures += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def load_cube(path: Path) -> tuple[int, np.ndarray]:
    size, vals = None, []
    for line in path.read_text().splitlines():
        s = line.strip()
        if s.startswith("LUT_3D_SIZE"):
            size = int(s.split()[1])
            continue
        parts = s.split()
        if len(parts) == 3 and not s.startswith("#"):
            try:
                vals.append([float(x) for x in parts])
            except ValueError:
                pass
    return size, np.array(vals, dtype=np.float64)


def parse_spi1d(path: Path) -> np.ndarray:
    vals = []
    for line in path.read_text().splitlines():
        token = line.strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", token):
            vals.append(float(token))
    return np.array(vals)


def config_matrix_for(text: str, name: str, window: int = 2500) -> np.ndarray | None:
    """First 16-value matrix inside the named colorspace block of a config
    text. Window search: a nested-lazy regex over these 1800-line files can
    backtrack catastrophically (one earlier revision hung 600 s on it)."""
    at = text.find(f"name: {name}")
    if at == -1:
        return None
    m = re.search(r"matrix: \[([^\]]+)\]", text[at: at + window])
    if not m:
        return None
    values = [float(v) for v in m.group(1).split(",")]
    if len(values) != 16:
        raise ValueError(f"matrix under {name!r} has {len(values)} values, expected 16")
    return np.array(values).reshape(4, 4)[:3, :3]


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------


def gate_v1_1() -> None:
    """Rebake v1.1.0 Standard at 64pt; compare against gripeyes' bake."""
    exe, params = _bake.compile_dctl(ROOT / "external/dctl/OpenDRT_v1.1.0.dctl")
    ref = ROOT / "external/reference/OpenDRT_v1.1_Standard__filmlight-egamut2-tloge_to_rec1886-64.cube"
    ours = _bake.bake_one(exe, params, "Standard", "rec1886", 64, "tlog-spi1d",
                          out=ROOT / "build/gate/v1.1-Standard-rec1886-64.cube")
    _, mine = load_cube(ours)
    _, theirs = load_cube(ref)
    if mine.shape != theirs.shape:
        report("gate v1.1 rec1886", False, f"shape {mine.shape} vs {theirs.shape}")
        return
    d = np.abs(mine - theirs).max()
    report("gate v1.1 rec1886 (64pt v gripeyes)", d <= 1e-4, f"max|delta| {d:.2e} over {len(mine)} samples")


def gate_v1_0() -> None:
    """Our v1.0.0 bakes vs Jed's shipped config cubes (SOFT gate)."""
    exe, params = _bake.compile_dctl(ROOT / "external/dctl/OpenDRT_v1.0.0.dctl")
    for look_token, look_name, display_token, file_label in (
        ("look0", "Default", "rec1886", "rec1886"),
        ("look0", "Default", "displayp3", "display-p3"),
    ):
        ref = (ROOT / f"external/OpenDRT_v1.0.0_full-config_v2/luts/"
               f"OpenDRT_v1.0.0_{look_name}__filmlight-egamut2-tloge_to_{file_label}.cube")
        ours = _bake.bake_one(exe, params, look_token, display_token, 48, "tlog-spi1d",
                              out=ROOT / f"build/gate/v1.0-{look_name}-{file_label}-48.cube")
        _, mine = load_cube(ours)
        _, theirs = load_cube(ref)
        d = np.abs(mine - theirs)
        report(f"gate v1.0 {look_name} -> {file_label} (48pt v Jed's cubes)", d.max() <= 6e-2,
               f"max|delta| {d.max():.4g}, mean {d.mean():.3g} (soft; his shipped cubes lag the v1.0.0 DCTL)")


def gate_spi1d() -> None:
    """ociogen-generated T-LogE spi1d vs Jed's shipped spi1d."""
    generated = parse_spi1d(ROOT / "build/ociogen/opendrt-ociogen-reference/transfer_functions/"
                            "oetf_filmlight_tloge_to_linear.spi1d")
    shipped = _bake.load_spi1d()
    n = min(len(generated), len(shipped))
    d = np.abs(generated[:n] - shipped[:n]).max()
    report("gate spi1d (ociogen v Jed's shipped)", d <= 1e-4, f"max|delta| {d:.2e} over {n} samples")


# --------------------------------------------------------------------------
# Config checks
# --------------------------------------------------------------------------


def check_config() -> None:
    import PyOpenColorIO as OCIO

    cfg = OCIO.Config.CreateFromFile(str(DIST / "config.ocio"))
    text = (DIST / "config.ocio").read_text()

    # The OpenDRT views are colorspace-views (gripeyes recipe): one colorspace
    # per look x display family plus one View line per display.
    labels = ("sRGB", "Display P3", "Rec.1886", "Rec.2100-PQ", "Rec.2100-HLG")
    cs = set(str(n) for n in cfg.getColorSpaceNames())
    expected = sorted(f"OpenDRT 1.1 {look} {label}" for look in _bake.LOOKS for label in labels)
    odrt = sorted(n for n in cs if n.startswith("OpenDRT 1.1"))
    report("OpenDRT view colorspaces present", odrt == expected,
           f"{len(odrt)}/{len(expected)} match exactly")

    wired = re.findall(r"!<View> \{name: OpenDRT 1\.1 [^,]+, colorspace: (OpenDRT 1\.1 [^\}]+)\}", text)
    report("display View wiring", sorted(set(wired)) == expected
           and len(wired) == 5 * len(_bake.LOOKS), f"{len(wired)} views wired")

    # roles: the python binding surfaces roles differently across OCIO
    # versions; use the API when it exists, config text otherwise.
    roles = {"scene_linear": "Linear Rec.709", "aces_interchange": "ACES2065-1",
             "cie_xyz_d65_interchange": "Linear CIE-XYZ D65", "data": "Non-Color"}
    for role, expected_role in roles.items():
        if hasattr(cfg, "getRole"):
            report(f"role {role}", cfg.getRole(role) == expected_role, f"{cfg.getRole(role)!r}")
        else:
            pattern = rf"(?m)^\s*{role}: {re.escape(expected_role)}$"
            report(f"role {role}", re.search(pattern, text) is not None, "in config text")

    built = 0
    for look in _bake.LOOKS:
        for label in labels:
            t = OCIO.ColorSpaceTransform(src="Linear Rec.709", dst=f"OpenDRT 1.1 {look} {label}")
            cfg.getProcessor(t).getDefaultCPUProcessor()
            built += 1
    report("OpenDRT processors build (scene_linear -> each view)", built == 5 * len(_bake.LOOKS),
           f"{built} processor chains")


def check_white_map() -> None:
    """ACES reference white maps to E-Gamut2 white through the spliced leg
    (BFD builtin + gamut matrix), and the grey axis is invariant. This is the
    frame contract the OpenDRT view chains assume."""
    import PyOpenColorIO as OCIO

    cfg = OCIO.Config.CreateFromFile(str(DIST / "config.ocio"))
    p = cfg.getProcessor(
        OCIO.ColorSpaceTransform(src="ACES2065-1", dst="Linear FilmLight E-Gamut 2")
    ).getDefaultCPUProcessor()
    white = np.array(p.applyRGB([1.0, 1.0, 1.0]))
    grey = np.array(p.applyRGB([0.18, 0.18, 0.18]))
    d_white = float(np.abs(white - 1.0).max())
    ok = d_white <= 1e-3 and float(np.abs(grey - grey.mean()).max()) <= 1e-4
    report("white map (ACES -> E-Gamut 2)", ok,
           f"white {np.round(white, 6).tolist()}, grey {np.round(grey, 6).tolist()}")


def check_spot_values() -> None:
    """Scene values through the full config vs the bake exe on the same points."""
    import PyOpenColorIO as OCIO

    cfg = OCIO.Config.CreateFromFile(str(DIST / "config.ocio"))
    eg = "Linear FilmLight E-Gamut 2"

    tlog_enc = cfg.getProcessor(
        OCIO.ColorSpaceTransform(src=eg, dst="Filmlight - E-Gamut2 - T-LogE")).getDefaultCPUProcessor()
    via_rec1886 = cfg.getProcessor(
        OCIO.ColorSpaceTransform(src=eg, dst="OpenDRT 1.1 Standard Rec.1886")).getDefaultCPUProcessor()

    points = np.array([
        [0.18, 0.18, 0.18],     # mid-grey
        [0.018, 0.018, 0.018],  # -3.3 stops over black
        [1.0, 1.0, 1.0],
        [8.0, 8.0, 8.0],
        [0.18, 0.0, 0.0],       # primaries in the E-Gamut frame
        [0.0, 0.18, 0.0],
        [0.0, 0.0, 0.18],
    ], dtype=np.float64)

    via_config = np.array([via_rec1886.applyRGB(p.tolist()) for p in points])
    tlog_pts = np.array([tlog_enc.applyRGB(p.tolist()) for p in points])

    exe, params = _bake.compile_dctl(ROOT / "external/dctl/OpenDRT_v1.1.0.dctl")
    args = ["--display_encoding_preset", str(_bake.option_index(params, "display_encoding_preset", "rec1886")),
            "--in_gamut", str(_bake.option_index(params, "in_gamut", "i_egamut2")),
            "--look_preset", "0",  # released DCTL: generic tokens; 0 = Standard
            "--in_oetf", str(_bake.option_index(params, "in_oetf", "ioetf_linear"))]
    proc = subprocess.run([str(exe), *args], input=points.astype(np.float32).tobytes(),
                          check=True, stdout=subprocess.PIPE)
    via_exe = np.frombuffer(proc.stdout, dtype=np.float32).reshape(-1, 3).astype(np.float64)

    d = np.abs(via_config - via_exe)
    report("spot values (config pipeline v bake exe)", d.max() <= 2e-3, f"7 points: max|delta| {d.max():.4g}")

    greys = via_config[:4]
    chroma = np.abs(greys - greys.mean(axis=1, keepdims=True)).max()
    report("neutral axis preserved", chroma <= 1e-4, f"grey-axis channel spread {chroma:.2e}")

    # Neutral greys through every display encoding's full chain (the wire
    # the user actually renders through on each display family).
    for label in ("sRGB", "Display P3", "Rec.1886", "Rec.2100-PQ", "Rec.2100-HLG"):
        cpu = cfg.getProcessor(
            OCIO.ColorSpaceTransform(src=eg, dst=f"OpenDRT 1.1 Standard {label}")
        ).getDefaultCPUProcessor()
        greys_all = np.array([cpu.applyRGB([g, g, g]) for g in (0.018, 0.18, 1.0, 8.0)])
        spread = float(np.abs(greys_all - greys_all.mean(axis=1, keepdims=True)).max())
        report(f"neutral axis through {label}", spread <= 1e-4, f"channel spread {spread:.2e}")

    for p, o in zip(points, via_config):
        print(f"    scene {np.round(p, 5).tolist()} -> rec1886 {np.round(o, 5).tolist()}")


# --------------------------------------------------------------------------
# Check images
# --------------------------------------------------------------------------

# A scene-linear chart: 9-stop exposure ramp + primaries + saturation sweep
# through a pink tint. Rendered per view to the sRGB display; eyeball next
# to the AgX render (the bundled config keeps AgX working side by side).


def render_check_images() -> None:
    import PyOpenColorIO as OCIO
    from PIL import Image

    cfg = OCIO.Config.CreateFromFile(str(DIST / "config.ocio"))
    h, w = 256, 512
    lin = np.zeros((h, w, 3), dtype=np.float32)
    ramp = 0.18 * 2.0 ** np.linspace(-4.5, 4.5, w).astype(np.float32)
    lin[:128, :, :] = ramp[None, :, None]
    prim = np.array([(0.18, 0, 0), (0, 0.18, 0), (0, 0, 0.18),
                     (0.18, 0.18, 0), (0, 0.18, 0.18), (0.18, 0, 0.18)], dtype=np.float32)
    for i, rgb in enumerate(prim):
        lin[128 + i * 6:134 + i * 6, :, :] = rgb[None, None, :]
    sat = np.linspace(-0.8, 0.8, w).astype(np.float32)
    tint = np.array([1.0, 0.45, 0.75], dtype=np.float32)
    lin[166:, :, :] = tint[None, None, :] * (0.18 * (1.0 + sat))[None, :, None]
    CHECKS.mkdir(parents=True, exist_ok=True)

    def save(tag: str, cpu) -> None:
        img = lin.copy().reshape(-1, 3)
        cpu.applyRGB(img.ravel().tolist())
        img = img.reshape(h, w, 3)
        Image.fromarray((np.clip(img, 0.0, 1.0) * 255.0).astype(np.uint8)
                        ).save(CHECKS / f"chart-{tag}.png")

    for display, view, tag in [
        ("sRGB", "AgX", "agx"),
        ("sRGB", "Standard", "standard-bundled"),
    ]:
        dv = OCIO.DisplayViewTransform(src="Linear FilmLight E-Gamut 2", display=display, view=view)
        save(tag, cfg.getProcessor(dv).getDefaultCPUProcessor())
    for cs, tag in [
        ("OpenDRT 1.1 Standard sRGB", "standard"),
        ("OpenDRT 1.1 Colorful sRGB", "colorful"),
        ("OpenDRT 1.1 Umbra sRGB", "umbra"),
        ("OpenDRT 1.1 Dystopic sRGB", "dystopic"),
    ]:
        t = OCIO.ColorSpaceTransform(src="Linear FilmLight E-Gamut 2", dst=cs)
        save(tag, cfg.getProcessor(t).getDefaultCPUProcessor())


def main() -> None:
    gate_v1_1()
    gate_v1_0()
    gate_spi1d()
    check_white_map()
    check_config()
    check_spot_values()
    render_check_images()
    if failures:
        print(f"\n{failures} check(s) FAILED - do not ship without resolving.")
        sys.exit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()