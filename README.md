# opendrt-1.1-ocio-workspace

Bakes Jed Smith's **OpenDRT v1.1.0** (https://github.com/jedypod/open-display-transform)
into OCIO view-transform LUTs and assembles a **Blender 5.2 drop-in config**.
No official v1.1.0 OCIO config exists (v1.0.0 shipped one; v1.1.0 ships only
DCTL/Resolve and Nuke files — jedypod/open-display-transform#43), so this
workspace makes one: the official DCTL is compiled to native code, baked into
the same T-LogE-shaped cubes Jed's own configs use, and spliced into
Blender's bundled 5.2 config.

## Layout

```
pixi.toml                the isolated env: conda g++/gxx, python 3.12,
                         PyOpenColorIO, ociogen (all resolved by pixi.lock)
scripts/
  10-fetch-artifacts.sh  pinned upstream sources + sha256 verification
  20-bake-luts.py        DCTL -> C++ (shim) -> native exe -> T-LogE lattice -> .cube
  30-build-config.py     runs ociogen; takes the generated spi1d + gamut
                         matrix; splices the drop-in config
  40-validate.py         fidelity gates + OCIO spot checks + check images
  dctl_shim.h            the piece of Resolve's DCTL runtime we replace
  ociogen-spec/          the minimal config.yaml we run ociogen with
external/                the pinned sources (created by 10-fetch)
build/                   intermediates (generated)
dist/OpenDRT-1.1-Blender5.2/   THE OUTPUT: copy this folder anywhere, point
                               Blender at its config.ocio
dist/OpenDRT-1.1-Blender5.2-checks/   rendered check images (eyeball)
```

## Use

```
pixi install
pixi run dist          # everything: fetch -> bake -> assemble -> validate
pixi run validate      # the evidence suite only
pixi run clean         # remove build/ and dist/
```

## What the config adds to Blender 5.2

Blender's bundled 5.2 config, otherwise untouched, plus:

- scene-side colorspaces `Linear FilmLight E-Gamut 2` and
  `Filmlight - E-Gamut2 - T-LogE` (the log shaper of the cubes),
- 35 view colorspaces: the 7 looks (Standard, Arriba, Sylvan, Colorful,
  Aery, Dystopic, Umbra) x 5 display encodings (sRGB 2.2-power, Display P3
  2.2-power, Rec.1886, Rec.2100-PQ @1000 nits, Rec.2100-HLG), each a plain
  colorspace whose chain ends at its display-encoded cube.

Wiring follows gripeyes/authored-DRTs (the pattern validated working in
Blender 5.2's OCIO by the user): each OpenDRT view is a colorspace whose
`from_scene_reference` is exactly `[CST(ACES2065-1 -> T-LogE), cube]`, with
the cube carrying the complete display transform and nothing after it. The
Rec.2020 display has no OpenDRT views (the DCTL has no matching SDR preset).

## Install for the Blender flatpak

```
cp -r dist/OpenDRT-1.1-Blender5.2 ~/.local/share/OpenColorIO/
flatpak override --user --filesystem=/home/zicklag/.local/share/OpenColorIO org.blender.Blender
flatpak override --user --env=OCIO=/home/zicklag/.local/share/OpenColorIO/OpenDRT-1.1-Blender5.2/config.ocio org.blender.Blender
```

Blender reads `OCIO` at startup (the bundled config is the fallback when the
variable is unset). Blender 5.0+ also honors `BLENDER_OCIO`, which scopes the
override to Blender alone — use it instead of `OCIO` if other OCIO apps
on flatpak should keep their own configs. To revert:

```
flatpak override --user --unset-env=OCIO --unset-filesystem=/home/zicklag/.local/share/OpenColorIO org.blender.Blender
```

- v1.1.0 Standard bake, 64pt, vs gripeyes' fully independent bake of the
  same release DCTL: max|delta| ~5e-6 over 262k samples. This pins the whole
  bake pipeline end to end.
- spot values through the assembled config vs straight through the bake exe:
  max ~3.5e-4 (cube interpolation vs exact lattice); grey axis perfectly
  neutral.
- white map: ACES reference white -> E-Gamut 2 white exactly, through the
  spliced BFD-legend (as gripeyes' config uses).
- neutral greys through every covered display encoding's full chain
  (sRGB, P3, Rec.1886, PQ, HLG): channel spread <= 2.6e-6.
- ociogen-generated T-LogE spi1d vs Jed's shipped one: identical.
- v1.0 48pt bakes vs Jed's shipped v1.0.0 cubes: ~5e-2 max in chromatic
  mids. Soft gate: his shipped v1.0.0 cubes pre/succeed the released
  v1.0.0 DCTL slightly; the v1.1 gate above is the hard one.

## Notes

- **Lattice**: cubes are gridded uniformly in T-LogE (the FilmLight log
  encoding with its upper range extended from 128 to 512), pre-decoded by
  Jed's shipped spi1d, with the DRT fed scene-linear. Found empirically
  against the shipped cubes; the DCTL's internal T-Log decode (a different,
  128-stop encoding) is refuted by the data.
- **Matrices**: Blender's own per-colorspace matrices are pure gamut maps in
  the physical XYZ-D65 interchange frame; ociogen's are in a Y-normalized
  frame. They differ exactly by the D65-white diagonal. The community
  Blender patch of OpenDRT 1.0 folds the white adapt into its matrix
  constants, so its printed numbers differ from ours for the same mapping.
- **Grid**: 64pt cubes, matching gripeyes' published bakes (the hard
  fidelity gate). `--size 48` bakes Jed's v1.0-config grid at 4x-fewer
  bytes if wanted.
- **Other looks/encodings**: the DCTL's remaining display presets (p3d60,
  p3dci, xyz, Dolby PQ) can be baked ad hoc, e.g. `pixi run python
  scripts/20-bake-luts.py --look Sylvan --display xyz --out
  build/luts/xyz.cube`. The srgb/displayp3 presets are pure 2.2-power
  encodings while Blender's sRGB/P3 display colorspaces use the piecewise
  sRGB curve; they agree closely except in deep shadows, and rec1886/PQ/HLG
  match Blender's displays exactly.
- **Licenses**: the OpenDRT DCTL is GPL-3.0; the LUT files Jed's own config
  zips distribute carry an MIT grant (see LICENSE.md in his zip); the
  ociogen source (bundled, vendored) is MIT. This workspace exists for
  personal use.

## Alternatives (other ways to get OpenDRT into OCIO apps)

If this workspace's approach (neutral 1.1.0 bake into Blender's own
convention) isn't what you need, these are the known options, as of
2026-10:

- **Jed's official v1.0.0 OCIO configs** —
  https://github.com/jedypod/open-display-transform/releases/tag/v1.0.0
  (`full-config` / `mini-config` zips): the only *official* OCIO configs,
  built with ociogen on the FilmLight E-Gamut2 reference. v1.0 math only;
  usable in Blender via OCIO env var as-is.
- **gripeyes/authored-DRTs** —
  https://github.com/gripeyes/authored-DRTs — personal, actively evolving
  config family built on v1.1.0: includes a faithful 64pt bake of the
  release **Standard** look (validated against our bake at 5e-6) plus his
  own authored look family; SDR-only, Houdini-flavored, deliberately
  "personally biased, not neutral". Pick this if you want his looks; pin a
  commit, the repo changes.
- **chrisbrejon/OpenDRT-OCIO-Config** and his DAS-UBER config —
  https://github.com/chrisbrejon/OpenDRT-OCIO-Config — single-file minimal
  OpenDRT 1.0.0 config, hand-assembled with colour-science, Blender-role
  friendly, sRGB-linear working space. Good if 1.0 + a light config is
  enough.
- **Blender PR 147492** —
  https://projects.blender.org/blender/blender/pulls/147492 — GeeDoubleU's
  unmerged upstream integration of OpenDRT 1.0 into Blender's bundled
  config; unmerged, but the patch (config chains + 48pt cubes) is the
  recipe reference for Blender-style wiring, mirrored here.
- **piotrus3333's Blender 5.0.1 config** —
  https://blenderartists.org/t/opendrt-for-blender-5/1626779 — community
  quick-start: Blender 5.0 config modified to expose OpenDRT (1.0) views.
- **MoazElgabry/ME_OpenDRT** OFX —
  https://github.com/MoazElgabry/ME_OpenDRT — OpenDRT v1.1.0 as an OFX
  plugin for Resolve (native C port of the DCTL). For Resolve users, not
  OCIO.
- **toodee.de's configs roundup** —
  https://www.toodee.de/2025/06/new-ocio-configs-available/ — a tracking
  post that indexes current OCIO configs, including OpenDRT generations.