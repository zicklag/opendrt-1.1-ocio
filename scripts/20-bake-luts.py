#!/usr/bin/env python3
"""
Bake the vendored OpenDRT DCTLs into .cube LUT files.

Resolve executes a DCTL per-pixel on the GPU: the host parses the
DEFINE_UI_PARAMS(...) wrappers into UI widgets and calls transform() while
those widgets are live globals. This script mirrors that host contract:

  1. preprocess — the DCTL text becomes a standalone C++ file:
       * scripts/dctl_shim.h supplies vector structs with component-wise
         operators, the _math intrinsics, and UI wrapper macros,
       * every live DEFINE_UI_PARAMS(...) line becomes a plain global with
         its default value (a shim macro turns any line that escapes this
         rewrite into a compile error, so nothing is silently skipped),
       * a small main() is appended that takes `--name value` pairs, assigns
         them to the UI globals, and streams float32 triplets from stdin
         through transform() to stdout.
  2. bake — g++ from the pixi env compiles that C++ once per DCTL version;
     a T-LogE lattice is streamed through the exe and written out as a
     .cube (R slowest / B fastest, standard cube ordering).

Lattice modes (--input-encoding):
    tlog-spi1d  lattice = T-LogE values, pre-decoded to linear with Jed's
                shipped spi1d, DRT fed via in_oetf=linear.
    tlog-dctl   lattice = T-LogE values, DCTL decodes them itself
                (in_oetf=filmlight_tlog).

40-validate.py gates the baker: our v1.0.0 bake must reproduce Jed's
shipped v1.0.0 cubes, and our v1.1.0 Standard bake must agree with
gripeyes' independent 64pt bake.

Usage:
    python scripts/20-bake-luts.py              # production: every look x display
    python scripts/20-bake-luts.py --list       # show a DCTL's parameter kinds
    python scripts/20-bake-luts.py --look Colorful --display display-p3 \
        --size 64 --out build/luts/adhoc.cube   # ad-hoc single bake
"""
import argparse
import re
import subprocess
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SHIM = Path(__file__).resolve().parent / "dctl_shim.h"
CXX = "g++"
BUILD = ROOT / "build"
LUTS = BUILD / "luts"
SPI1D = (ROOT / "external/OpenDRT_v1.0.0_full-config_v2/transfer_functions/"
         "oetf_filmlight_tloge_to_linear.spi1d")
# The seven visible look presets of OpenDRT v1.1.0, in the DCTL's
# look_preset combo order (generic tokens look0..look6 in the release
# DCTL); the names match the tooltips in OpenDRT_v1.1.0.nk and the release
# notes. INDEX into the combo = the look, since the release's knob list is
# generic; validation output is named through LOOKS.
LOOKS = ["Standard", "Arriba", "Sylvan", "Colorful", "Aery", "Dystopic", "Umbra"]

# Production display encodings — only the two the Blender drop-in uses:
# rec1886 (2.4-power; used for EVERY SDR display — sRGB, Display P3,
# Rec.1886, Rec.2020 — since Blender's own display colorspaces do the final
# encoding; the recipe the community Blender integration of OpenDRT 1.0
# ships) and rec2100-pq at 1000 nits for HDR. The DCTL's other seven display
# presets (srgb, displayp3-2.2-power, p3d60, p3dci, xyz, HLG, Dolby PQ) can
# be baked ad hoc with --display/--set; note the DCTL's "displayp3" is
# 2.2-power, which does NOT match Blender's sRGB-piecewise Display P3
# colorspace — hence it is not in the default set.
DISPLAYS = {
    "rec1886": {"display_encoding_preset": "rec1886", "tn_Lp": 100.0},
    "rec2100-pq": {"display_encoding_preset": "rec2100pq", "tn_Lp": 1000.0},
}
SIZE = 48
INPUT_MODE = "tlog-spi1d"

# --------------------------------------------------------------------------
# DCTL parsing
# --------------------------------------------------------------------------

UI_PARAM = re.compile(r"(?m)^(\s*)DEFINE_UI_PARAMS\(")
UI_TYPES = {  # DCTL UI kind -> C type (combo/check = int, float slider = float)
    "DCTLUI_COMBO_BOX": "int",
    "DCTLUI_CHECK_BOX": "int",
    "DCTLUI_SLIDER_FLOAT": "float",
    "DCTLUI_SLIDER_INT": "int",
}


def split_top_level(text: str) -> list[str]:
    """Split a macro argument list on top-level commas, respecting braces and quotes."""
    args, cur, depth, in_str = [], [], 0, False
    for ch in text:
        if in_str:
            cur.append(ch)
            if ch in "\"'":
                in_str = False
        elif ch in "\"'":
            cur.append(ch)
        elif ch in "{([":
            depth += 1
            cur.append(ch)
        elif ch in "})]":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    args.append("".join(cur).strip())
    return args


def macro_close(text: str, open_idx: int) -> int:
    """Index of the ')' closing the macro call whose '(' is at open_idx."""
    depth = 0
    for i in range(open_idx, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unterminated macro call")


def ui_param_args(dctl: str, match: re.Match) -> list[str]:
    """Arguments of the DEFINE_UI_PARAMS( call matched by match."""
    open_idx = match.end() - 1  # the '(' of the call
    return split_top_level(dctl[open_idx + 1: macro_close(dctl, open_idx)])


def ui_params(dctl: str) -> dict[str, dict]:
    """Parse each live DEFINE_UI_PARAMS line into a parameter record."""
    params = {}
    for match in UI_PARAM.finditer(dctl):
        args = ui_param_args(dctl, match)
        name, kind, default = args[0], args[2], args[3]
        if kind not in UI_TYPES:
            raise ValueError(f"unknown UI kind {kind!r} on parameter {name!r}")
        record = {"type": UI_TYPES[kind], "default": default}
        if kind != "DCTLUI_SLIDER_FLOAT" and record["default"].endswith(("f", "F")):
            record["default"] = record["default"][:-1]
        if kind == "DCTLUI_COMBO_BOX":
            record["options"] = [o.strip() for o in args[4].strip(" {}").split(",")]
        params[name] = record
    return params


def option_index(params: dict, name: str, token: str) -> int:
    """Resolve a combo token like i_egamut2 / rec2100pq to its index."""
    for i, opt in enumerate(params[name]["options"]):
        if opt.lower() == token.lower():
            return i
    raise ValueError(f"token {token!r} not in {name} options {params[name]['options']}")


# --------------------------------------------------------------------------
# C generation
# --------------------------------------------------------------------------


def render_main(params: dict[str, dict]) -> str:
    """The main() the bake appends: assigns --name value pairs, bakes stdin."""
    assigned = []
    for name, p in params.items():
        value = "strtof(val, NULL)" if p["type"] == "float" else "strtol(val, NULL, 0)"
        assigned.append(f'    else if (!strcmp(name, "{name}")) {name} = {value};')
    first = assigned[0].replace("    else if", "    if") if assigned else ""
    chain = first + ("".join(assigned[1:]) if len(assigned) > 1 else "")
    return f"""
/* main(): the only code the bake adds. It assigns --name value pairs to the
 * UI globals, then streams float32 r,g,b triplets from stdin through
 * transform() to stdout. Dummy width/height/position: the transform is
 * resolution independent when the curve editor is disabled. */
static void set_param(const char *name, const char *val) {{
{chain} else {{
        fprintf(stderr, "bake exe: unknown parameter '%s'\\n", name);
        exit(2);
    }}
}}

int main(int argc, char **argv) {{
    for (int i = 1; i + 1 < argc; i += 2)
        set_param(argv[i] + (argv[i][0] == '-' && argv[i][1] == '-' ? 2 : 0), argv[i + 1]);
    float rgb[3];
    while (fread(rgb, 4, 3, stdin) == 3) {{
        float3 out = transform(1, 1, 0, 0, rgb[0], rgb[1], rgb[2]);
        float packed[3] = {{out.x, out.y, out.z}};
        fwrite(packed, 4, 3, stdout);
    }}
    return ferror(stdin) || ferror(stdout);
}}
"""


def render_c(dctl_text: str, provenance: str) -> tuple[str, dict]:
    """Preprocess the DCTL text into a standalone C++ file with a baking main()."""
    params = ui_params(dctl_text)

    # Splice each DEFINE_UI_PARAMS(...) call into a plain global definition.
    # Index arithmetic, not line matching: several wrappers span many lines.
    out, last = [], 0
    for match in UI_PARAM.finditer(dctl_text):
        args = ui_param_args(dctl_text, match)  # also validates the extent
        open_idx = match.end() - 1
        close_idx = macro_close(dctl_text, open_idx)
        line_end = dctl_text.find("\n", close_idx)
        out.append(dctl_text[last: match.start()])
        name, default = args[0], args[3]
        ctype = UI_TYPES[args[2]]
        suffix = "f" if ctype == "float" and not default.endswith(("f", "F")) else ""
        out.append(f"{match.group(1)}static {ctype} {name} = {default}{suffix};")
        out.append(dctl_text[close_idx + 1: line_end] if line_end != -1 else "")
        last = line_end + 1
    out.append(dctl_text[last:])
    body = "".join(out)
    # Drop Resolve's #line directives: they describe the DCTL's own file and
    # would make gcc report shim-offset line numbers here.
    body = "\n".join(line for line in body.splitlines()
                     if not line.lstrip().startswith("#line"))

    source = (f"/* generated from {provenance}; edit the DCTL, not this file */\n"
              f"{SHIM.read_text()}\n{body}{render_main(params)}")
    return source, params


def compile_dctl(dctl_path: Path, build_dir: Path = BUILD) -> tuple[Path, dict]:
    """Preprocess + compile one DCTL version into a reusable bake exe."""
    build_dir.mkdir(parents=True, exist_ok=True)
    src = build_dir / f"{dctl_path.stem.lower()}.cpp"
    exe = build_dir / f"bake_{dctl_path.stem.lower()}"
    source, params = render_c(dctl_path.read_text(), provenance=str(dctl_path))
    src.write_text(source)
    result = subprocess.run([CXX, "-std=gnu++17", "-O2", "-o", str(exe), str(src), "-lm"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        errors = "\n".join(line for line in result.stderr.splitlines()
                           if "error:" in line)[-1600:]
        raise RuntimeError(f"{CXX} failed on {src}:\n{errors}")
    return exe, params


# --------------------------------------------------------------------------
# Baking
# --------------------------------------------------------------------------


def version_of(exe: Path) -> str:
    """OpenDRT_v1.1.0-style version string from the exe file name. Path.stem
    would cut at '.0', so strip the prefix from the full name instead."""
    return "OpenDRT_" + exe.name[len("bake_opendrt_"):]


def load_spi1d() -> np.ndarray:
    """Jed's shipped T-LogE decode table (header lines carry >1 token)."""
    if not SPI1D.exists():
        raise FileNotFoundError(f"missing {SPI1D}; run scripts/10-fetch-artifacts.sh")
    vals = []
    for line in SPI1D.read_text().splitlines():
        token = line.strip()
        # Every header line carries a keyword (>= 2 tokens) or braces; the
        # 8192 data lines are one bare float each.
        if re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", token):
            vals.append(float(token))
    arr = np.array(vals)
    if len(arr) == 0:
        raise ValueError(f"{SPI1D}: no samples parsed")
    return arr


def lattice(size: int, mode: str, spi1d: np.ndarray) -> np.ndarray:
    """The T-LogE lattice fed to the DRT, in .cube order (R fastest)."""
    axes = np.linspace(0.0, 1.0, size, dtype=np.float32)
    grid = np.stack(np.meshgrid(axes, axes, axes, indexing="ij"), axis=-1)  # (r,g,b,3)
    if mode == "tlog-dctl":
        flat = grid  # DCTL decodes T-LogE itself
    elif mode == "tlog-spi1d":
        flat = np.interp(grid, np.linspace(-0.15, 1.2, len(spi1d)), spi1d).astype(np.float32)
    else:
        raise ValueError(f"unknown lattice mode {mode!r}")
    # .cube ordering: B slowest, R fastest — transpose before flattening.
    return flat.transpose(2, 1, 0, 3).reshape(-1, 3)
def write_cube(out: Path, rgb: np.ndarray, size: int, comment: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        f.write(f"# {comment}\nLUT_3D_SIZE {size}\n\n")
        np.savetxt(f, rgb, fmt="%.6g", delimiter=" ")


def bake_one(
    exe: Path,
    params: dict,
    look: str,
    display: str,
    size: int,
    mode: str,
    sets: list[str] | None = None,
    out: Path | None = None,
) -> Path:
    """Bake one look at one display encoding; return the .cube path."""
    args: list[str] = []
    # The release DCTL names its looks look0..look6; LOOKS.index() gives the
    # position, which doubles as the combo index (names documented in the nk
    # tooltips and release notes; see the LOOKS comment above).
    args += ["--in_gamut", str(option_index(params, "in_gamut", "i_egamut2"))]
    if look in LOOKS:
        args += ["--look_preset", str(LOOKS.index(look))]
    else:
        args += ["--look_preset", str(option_index(params, "look_preset", look))]
    # Display manifest keys ("display-p3") differ from DCTL combo tokens
    # ("displayp3"); resolve through the manifest, or accept a raw DCTL
    # token for ad-hoc bakes of the presets that aren't in the manifest.
    token = (DISPLAYS[display]["display_encoding_preset"] if display in DISPLAYS
             else display)
    args += ["--display_encoding_preset",
             str(option_index(params, "display_encoding_preset", token))]
    if mode == "tlog-spi1d":
        # The lattice is pre-decoded outside the DRT: feed it as scene-linear.
        args += ["--in_oetf", str(option_index(params, "in_oetf", "ioetf_linear"))]
    else:
        args += ["--in_oetf", str(option_index(params, "in_oetf", "ioetf_filmlight_tlog"))]
    if display in DISPLAYS:
        args += ["--tn_Lp", str(DISPLAYS[display]["tn_Lp"])]
    for extra in sets or []:
        name, _, value = extra.partition("=")
        args += [f"--{name}", value]

    spi1d = load_spi1d() if mode == "tlog-spi1d" else np.zeros(0)
    stream = lattice(size, mode, spi1d)
    proc = subprocess.run([str(exe), *args], input=stream.tobytes(), check=True, stdout=subprocess.PIPE)
    rgb = np.frombuffer(proc.stdout, dtype=np.float32).reshape(-1, 3)
    if out is None:
        out = LUTS / f"{version_of(exe)}_{look}__filmlight-egamut2-tloge_to_{display}.cube"
    write_cube(out, rgb, size, f"Baked by opendrt-1.1-ocio-workspace from {exe.name}, lattice {mode}")
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cli() -> None:
    ap = argparse.ArgumentParser(description="Bake OpenDRT DCTLs to .cube LUTs")
    ap.add_argument("--dctl", type=Path, default=ROOT / "external/dctl/OpenDRT_v1.1.0.dctl")
    ap.add_argument("--look", help="single look token; omit for the production set")
    ap.add_argument("--display", help="single display token")
    ap.add_argument("--size", type=int, default=SIZE, help="3D LUT grid resolution")
    ap.add_argument("--input-encoding", default=INPUT_MODE, choices=["tlog-spi1d", "tlog-dctl"])
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                    help="extra UI parameter override, e.g. --set tn_Lp=1000")
    ap.add_argument("--out", type=Path, help="output path for a single bake")
    ap.add_argument("--list", action="store_true", help="list the DCTL's parameter kinds and exit")
    args = ap.parse_args()

    exe, params = compile_dctl(args.dctl)
    if args.list:
        for name, p in params.items():
            if "options" in p:
                print(f"{name}: {', '.join(p['options'])}")
            else:
                print(f"{name}: {p['type']} (default {p['default']})")
        return

    if args.look:
        out = bake_one(exe, params, args.look, args.display, args.size, args.input_encoding,
                       sets=args.set, out=args.out)
        print(f"baked {out.relative_to(ROOT)}")
        return

    for look in LOOKS:
        for display in DISPLAYS:
            out = bake_one(exe, params, look, display, args.size, args.input_encoding)
            print(f"baked {out.relative_to(ROOT)}")


if __name__ == "__main__":
    cli()