#!/usr/bin/env python3
"""
Assemble dist/OpenDRT-1.1-Blender5.2: Blender 5.2's bundled OCIO config with
OpenDRT 1.1 view transforms added.

Pipeline position: 10-fetch (pinned sources) -> 20-bake (cubes) -> THIS ->
40-validate (gates + spot checks).

Steps:
  1. Run Jed's ociogen (vendored at the pinned commit) with
     scripts/ociogen-spec/config.yaml. We consume two things from its
     output, both computed by his generator from FilmLight's published
     chromaticities (no hand math):
       - the freshly generated oetf_filmlight_tloge_to_linear.spi1d,
       - the XYZ(D65) -> E-Gamut 2 gamut matrix.
  2. Stage the drop-in folder: Blender 5.2's bundled colormanagement tree
     (config.ocio + icc/, luts/, filmic/) plus luts/opendrt/ holding our
     baked cubes and that spi1d.
  3. Splice into config.ocio, in Blender 5.2 syntax:
       * scene-side colorspaces "Linear FilmLight E-Gamut 2" and
         "Filmlight - E-Gamut2 - T-LogE" (the shaper of the OpenDRT cubes),
       * one view transform per look x display encoding: CST from the scene
         reference (ACES2065-1) to T-LogE, the tetrahedral cube, then CST
         from the display encoding to cie_xyz_d65_interchange — the same
         shape the bundled AgX/ACES 2.0 view transforms use,
       * per-display views (plus - SDR views on the HDR displays),
       * the extended active_views list and a provenance heading.
  4. Fail loudly if any anchor is missing or any transform src unresolved.

Empirical note on matrices (established against the real configs): Blender's
own per-colorspace matrices are pure gamut maps in the *physical* XYZ-D65
interchange frame; ociogen emits its matrices in a Y-normalized XYZ frame.
They are related by the D65-white diagonal
    D = diag(1/0.9504559271051541, 1, 1/1.0890577507598784)
(physical <-> Y-normalized). The composition ociogen_matrix @ D reproduces,
to file precision, both the community Blender integration's E-Gamut 2 matrix
and, applied the same way, Blender's own E-Gamut(v1) colorspace matrix —
checked numerically in 40-validate.py.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import importlib.util
_spec = importlib.util.spec_from_file_location("bake_luts", ROOT / "scripts/20-bake-luts.py")
_bake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bake)
DISPLAYS, LOOKS = _bake.DISPLAYS, _bake.LOOKS
DIST = ROOT / "dist"
DIST_CONFIG_NAME = "OpenDRT-1.1-Blender5.2"
BLENDER_SRC = ROOT / "external/blender-5.2"
OCIOGEN_SPEC = ROOT / "scripts/ociogen-spec/config.yaml"
OCIOGEN_OUT = ROOT / "build/ociogen/opendrt-ociogen-reference"

# Standard CIE 1931 2-degree D65 white point, Y-normalized; the frame Blender's
# own colorspaces (and their interop interchange role) are expressed in.
D65_WHITE = np.array([0.3127 / 0.329, 1.0, (1 - 0.3127 - 0.329) / 0.329])

# Cube display token -> the display encoding the chain converts from (the
# view-transform name suffix matches: rec1886 = plain, PQ = - HDR 1000 nits).
DISPLAY_ENCODING_OF = {
    "rec1886": "Rec.1886",
    "rec2100-pq": "Rec.2100-PQ",
}
# The Blender displays that get OpenDRT views: every SDR display via the
# rec1886 cubes, PQ with its own cubes, HLG with - SDR views only.
ALL_BLENDERS_DISPLAYS = ["sRGB", "Display P3", "Rec.1886", "Rec.2020", "Rec.2100-PQ", "Rec.2100-HLG"]
VENDOR_PREFIX = "opendrt/"
CUBES_SUBDIR = "luts/opendrt"

PROVENANCE = """# OpenDRT 1.1 view transforms added by opendrt-1.1-ocio-workspace:
# base Blender 5.2 bundled config (blender-v5.2-release
# 35fea4a8ceacb061155b49eb0aecda55a287ee67); cubes baked from the official
# OpenDRT_v1.1.0.dctl and cross-validated by scripts/40-validate.py.
"""


# --------------------------------------------------------------------------
# ociogen artifacts
# --------------------------------------------------------------------------


def run_ociogen() -> None:
    """Vendor ociogen if needed, then regenerate the reference artifacts."""
    probe = subprocess.run([sys.executable, "-c", "import ociogen"], capture_output=True)
    if probe.returncode != 0:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-deps",
                        str(ROOT / "external/ociogen")], check=True)
    subprocess.run(["ociogen", "run", "--config", str(OCIOGEN_SPEC)], check=True)


def ociogen_artifacts() -> tuple[list[float], Path]:
    """Parse the generated config: the XYZ->E-Gamut2 matrix and the spi1d."""
    generated = OCIOGEN_OUT / "opendrt-ociogen-reference_ocio-v2.0.ocio"
    text = generated.read_text()
    block = re.search(r"- !<ColorSpace>\n    name: XYZ D65\n(.*?)(?=\n  - !<ColorSpace>|\Z)",
                      text, re.S)
    if not block:
        raise RuntimeError("ociogen output has no 'XYZ D65' colorspace")
    matrix = re.search(r"matrix: \[([^\]]+)\]", block.group(1))
    if not matrix:
        raise RuntimeError("'XYZ D65' colorspace carries no matrix")
    values = [float(v) for v in matrix.group(1).split(",")]
    if len(values) != 16:
        raise RuntimeError(f"gamut matrix has {len(values)} values, expected 16")
    m = np.array(values).reshape(4, 4)
    if np.abs(m[:3, 3]).max() != 0 or not np.allclose(m[3], [0.0, 0.0, 0.0, 1.0]):
        raise RuntimeError(f"unexpected affine part in gamut matrix: {m}")
    # ociogen's matrix targets its Y-normalized XYZ frame; Blender's matrices
    # are written against the physical D65 interchange frame. The white
    # diagonal D converts the frames; verify.py also checks this against the
    # community integration's known-constant values.
    physical = (m[:3, :3] @ np.diag(1.0 / D65_WHITE)).flatten()  # 12 numbers
    spi1d = (OCIOGEN_OUT / "transfer_functions/oetf_filmlight_tloge_to_linear.spi1d")
    if not spi1d.exists():
        raise RuntimeError("ociogen did not generate oetf_filmlight_tloge_to_linear.spi1d")
    return [f"{v:.15g}" for v in physical.flatten().tolist()], spi1d


# --------------------------------------------------------------------------
# Block templates (Blender 5.2 syntax)
# --------------------------------------------------------------------------


def colorspace_blocks(matrix: list[float]) -> str:
    """The two scene-side colorspaces every OpenDRT view chain depends on."""
    cells = ", ".join(matrix)
    return f"""
  - !<ColorSpace>
    name: Linear FilmLight E-Gamut 2
    aliases: [Linear FilmLight E-Gamut 2 I-D65, "FilmLight: Linear - E-Gamut 2"]
    family: Linear
    equalitygroup:
    bitdepth: 32f
    isdata: false
    description: |
      Linear FilmLight E-Gamut 2, adapted to the D65 white point. The
      reference gamut of Jed Smith's OpenDRT; primaries follow FilmLight's
      published E-Gamut 2 spec, matrix generated by ociogen.
    from_scene_reference: !<GroupTransform>
      children:
        - !<ColorSpaceTransform> {{src: ACES2065-1, dst: Linear CIE-XYZ I-D65}}
        - !<MatrixTransform> {{matrix: [{cells}, 0, 0, 0, 0, 0, 0, 1]}}

  - !<ColorSpace>
    name: Filmlight - E-Gamut2 - T-LogE
    family: Log
    equalitygroup:
    bitdepth: 32f
    isdata: false
    encoding: log
    description: |
      FilmLight E-Gamut 2 in T-LogE (T-Log with upper range extended from
      128 to 512). The shaper encoding of the OpenDRT 1.1 view-transform
      cubes; spi1d transfer function generated by ociogen.
    from_scene_reference: !<GroupTransform>
      children:
        - !<ColorSpaceTransform> {{src: ACES2065-1, dst: Linear FilmLight E-Gamut 2}}
        - !<FileTransform> {{src: {VENDOR_PREFIX}oetf_filmlight_tloge_to_linear.spi1d, interpolation: linear, direction: inverse}}
    to_scene_reference: !<GroupTransform>
      children:
        - !<FileTransform> {{src: {VENDOR_PREFIX}oetf_filmlight_tloge_to_linear.spi1d, interpolation: linear}}
        - !<ColorSpaceTransform> {{src: Linear FilmLight E-Gamut 2, dst: ACES2065-1}}
"""


def cube_name(look: str, display: str) -> str:
    return VENDOR_PREFIX + f"OpenDRT_v1.1.0_{look}__filmlight-egamut2-tloge_to_{display}.cube"


def view_transform_blocks() -> str:
    """One view transform per look x baked display encoding."""
    blocks = []
    for look in LOOKS:
        for display, token in DISPLAYS.items():
            anchor = DISPLAY_ENCODING_OF[display]
            suffix = {"rec1886": "", "display-p3": " Display P3",
                      "rec2100-pq": " - HDR 1000 nits"}[display]
            kind = "HDR" if display == "rec2100-pq" else "SDR"
            blocks.append(f"""
  - !<ViewTransform>
    name: OpenDRT 1.1 {look}{suffix}
    family: OpenDRT
    description: |
      OpenDRT v1.1.0 '{look}' look for {anchor} ({kind}, {token}), T-LogE-shaped
      48pt cube baked from the official OpenDRT_v1.1.0.dctl.
    from_scene_reference: !<GroupTransform>
      children:
        - !<ColorSpaceTransform> {{src: ACES2065-1, dst: Filmlight - E-Gamut2 - T-LogE}}
        - !<FileTransform> {{src: {cube_name(look, display)}, interpolation: tetrahedral}}
        - !<ColorSpaceTransform> {{src: {anchor}, dst: cie_xyz_d65_interchange}}
""")
    return "".join(blocks)


def view_lines(display: str) -> str:
    """View entries added to one display, just after its Standard view.
    Every SDR display gets the plain rec1886-cube view transforms (the
    Blender display colorspace handles that display's encoding, the way
    Jed's Blender integration and bundled AgX views work). PQ gets its
    - HDR 1000 nits views plus - SDR; HLG gets - SDR only.
    """
    lines = []
    for look in LOOKS:
        lines.append(
            f"    - !<View> {{name: OpenDRT 1.1 {look}, view_transform: OpenDRT 1.1 {look}, display_colorspace: {display}}}")
    if display == "Rec.2100-PQ":
        for look in LOOKS:
            lines.append(
                f"    - !<View> {{name: OpenDRT 1.1 {look} - HDR 1000 nits, view_transform: OpenDRT 1.1 {look} - HDR 1000 nits, display_colorspace: {display}}}")
        for look in LOOKS:
            lines.append(
                f"    - !<View> {{name: OpenDRT 1.1 {look} - SDR, view_transform: OpenDRT 1.1 {look}, display_colorspace: {display}}}")
    if display == "Rec.2100-HLG":
        for look in LOOKS:
            lines.append(
                f"    - !<View> {{name: OpenDRT 1.1 {look} - SDR, view_transform: OpenDRT 1.1 {look}, display_colorspace: {display}}}")
    return "\n".join(lines) + "\n"




def insert_before_section(text: str, header: str, payload: str) -> str:
    """Insert payload just before a top-level `header:` section."""
    anchor = f"\n{header}\n"
    if text.count(anchor) != 1:
        raise RuntimeError(f"anchor {header!r} not found exactly once in config")
    return text.replace(anchor, f"{payload}{header}\n")




def insert_after_line(text: str, line: str, payload: str) -> str:
    anchor = f"{line}\n"
    if text.count(anchor) != 1:
        raise RuntimeError(f"anchor line not found exactly once: {line!r}")
    return text.replace(anchor, anchor + payload)




def splice(text: str, matrix: list[float]) -> str:
    """Apply all insertions to config.ocio text; return the new text with provenance."""
    text = insert_before_section(text, "looks:", colorspace_blocks(matrix))
    text = insert_before_section(text, "colorspaces:", view_transform_blocks())
    for display in ALL_BLENDERS_DISPLAYS:
        standard = (f"    - !<View> {{name: Standard, view_transform: Standard, "
                    f"display_colorspace: {display}}}")
        text = insert_after_line(text, standard, view_lines(display))
    text = PROVENANCE + text

    # Extend active_views with every OpenDRT view name we added.
    names = sorted(set(re.findall(r"\{name: (OpenDRT 1\.1 [^,]+),", text)))
    old = re.search(r"active_views: \[([^\]]*)\]", text)
    if not old:
        raise RuntimeError("active_views list not found")
    additions = [n for n in names if n not in old.group(1)]
    text = text.replace(old.group(0), f"active_views: [{old.group(1)}, " + ", ".join(additions) + "]")
    return text


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def stage_dist() -> Path:
    """Fresh dist folder from Blender's tree + our cubes + the generated spi1d."""
    dist = DIST / DIST_CONFIG_NAME
    if dist.exists():
        shutil.rmtree(dist)
    shutil.copytree(BLENDER_SRC, dist, dirs_exist_ok=False)
    cubes_target = dist / CUBES_SUBDIR
    cubes_target.mkdir(parents=True)
    for cube in sorted((ROOT / "build/luts").glob("OpenDRT_v1.1.0_*.cube")):
        shutil.copy2(cube, cubes_target / cube.name)
    _, spi1d = ociogen_artifacts()
    shutil.copy2(spi1d, cubes_target / spi1d.name)
    return dist


def main() -> None:
    print("[1/4] ociogen reference artifacts...")
    run_ociogen()

    print("[2/4] staging dist folder...")
    dist = stage_dist()
    matrix, _ = ociogen_artifacts()

    print("[3/4] splicing config.ocio...")
    config_path = dist / "config.ocio"
    spliced = splice(config_path.read_text(), matrix)
    config_path.write_text(spliced)

    print("[4/4] checking every FileTransform src resolves via search_path...")
    search = re.search(r'search_path: "?([^"\n]+)"?', spliced)
    dirs = [d for d in search.group(1).split(":") if d]
    for m in re.finditer(r"src: ([^\s,}]+)", spliced):
        src = m.group(1)
        if not any(src.endswith(ext) for ext in (".cube", ".spi1d", ".spimtx", ".icc")):
            continue
        found = any((dist / d / src).exists() for d in dirs) or (dist / src).exists()
        if not found:
            raise RuntimeError(f"unresolved transform src {src!r} in search dirs {dirs}")

    print(f"OK: {dist.relative_to(ROOT)}; config {config_path.stat().st_size} bytes; "
          f"{len(LOOKS)} looks x {len(DISPLAYS)} displays baked, spliced, and staged.")


if __name__ == "__main__":
    main()