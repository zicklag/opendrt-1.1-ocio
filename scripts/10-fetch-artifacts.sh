#!/usr/bin/env bash
# Fetch and pin the exact upstream artifacts this build is allowed to use.
#
# Pinning, two ways:
#   * jedypod release assets: immutable URLs whose sha256 is checked on
#     every run. The v1.0.0 full-config zip is pinned whole, so every LUT/
#     spi1d member inside it is pinned transitively.
#   * git-sourced artifacts (Blender's colormanagement dir, gripeyes'
#     Standard cube for cross-checking, ociogen's source): fetched at one
#     exact commit SHA each, so re-runs see identical content even though
#     the branches keep moving.
#
# Any wrong hash exits loudly, instead of silently baking LUTs from
# different math than what this workspace validated against.

set -euo pipefail
cd "$(dirname "$0")/.." # workspace root

fetch() { # fetch <url> <dest> — retries ride out the raw CDN's occasional 5xx/empty replies
    curl -fsSL --retry 8 --retry-all-errors --retry-delay 3 "$1" -o "${2:?fetch needs a dest}"
}

mkdir -p external/dctl external/reference external/blender-5.2

# --- OpenDRT DCTL sources (the exact math we bake) ----------------------
fetch "https://github.com/jedypod/open-display-transform/releases/download/v1.1.0/OpenDRT_v1.1.0.dctl" \
    external/dctl/OpenDRT_v1.1.0.dctl
fetch "https://github.com/jedypod/open-display-transform/releases/download/v1.0.0/OpenDRT_v1.0.0.dctl" \
    external/dctl/OpenDRT_v1.0.0.dctl
fetch "https://github.com/jedypod/open-display-transform/releases/download/v1.1.0/OpenDRT_v1.1.0.nk" \
    external/dctl/OpenDRT_v1.1.0.nk


# --- Jed's official v1.0.0 OCIO config zip ------------------------------
# Reference artifacts for the fidelity gate in 40-validate.py: the shipped
# 48pt cubes are the author's own bakes, and our baker must reproduce them.
fetch "https://github.com/jedypod/open-display-transform/releases/download/v1.0.0/OpenDRT_v1.0.0_full-config_v2.zip" \
    external/openDRT_v1.0.0_full-config_v2.zip
unzip -oq external/openDRT_v1.0.0_full-config_v2.zip -d external/

# --- gripeyes' independently baked v1.1.0 Standard cube -------------------
# 64pt bakes of the same preset, from a different author and different
# source pass; used as a second fidelity gate in 40-validate.py.
GRIPEYES_SHA=8853bd0254bc0ecc779bc12ee11678f1656819c7
fetch "https://raw.githubusercontent.com/gripeyes/authored-DRTs/$GRIPEYES_SHA/OpenDRT/OpenDRT_v1.1_Standard__filmlight-egamut2-tloge_to_rec1886.cube" \
    external/reference/OpenDRT_v1.1_Standard__filmlight-egamut2-tloge_to_rec1886-64.cube

# --- ociogen, Jed's OCIO config generator --------------------------------
# Vendored at the pinned commit; installed into the pixi env (no deps; we
# satisfy them with conda packages) and used to independently regenerate
# the T-LogE transfer function, as a math agreement check.
OCIOGEN_SHA=8aaed4366ffbca4d2d664da9a088a5cc4d0e340f
fetch "https://github.com/jedypod/ociogen/archive/$OCIOGEN_SHA.tar.gz" external/ociogen.tar.gz
rm -rf external/ociogen
mkdir -p external/ociogen
tar -xzf external/ociogen.tar.gz -C external/ociogen --strip-components=1

# --- Blender 5.2's bundled colormanagement directory ---------------------
# The base of the drop-in config. Everything under release/datafiles/
# colormanagement/ at the pinned commit: config.ocio plus its icc/, luts/
# and filmic/ search-path directories.
BLENDER_SHA=35fea4a8ceacb061155b49eb0aecda55a287ee67
CM_BASE="release/datafiles/colormanagement"
list_cm() { # list files in one dir of Blender's colormanagement tree
    curl -fsSL "https://api.github.com/repos/blender/blender/contents/$1?ref=$BLENDER_SHA" |
        jq -r '.[] | select(.type == "file") | .path'
}
# config.ocio first; the three search-path dirs afterwards.
for dir in "$CM_BASE" "$CM_BASE/luts" "$CM_BASE/filmic" "$CM_BASE/icc"; do
    while IFS= read -r path; do
        if [ "$dir" = "$CM_BASE" ]; then
            dest="external/blender-5.2/$(basename "$path")"
        else
            dest="external/blender-5.2/${path#"$CM_BASE"}"
        fi
        mkdir -p "$(dirname "$dest")"
        fetch "https://raw.githubusercontent.com/blender/blender/$BLENDER_SHA/$path" "$dest"
        echo "fetched blender-5.2/${dest#external/blender-5.2/}"
    done < <(list_cm "$dir")
done

# --- Verify the immutability pins -----------------------------------------
# NOTE: after tag v1.1.0, Jed re-uploaded the release DCTL; today's asset is
# byte-identical to repo main's display-transforms/opendrt/OpenDRT.dctl
# (checked via raw.githubusercontent). Changes since the tag: AP0/AP1 CAT
# fix, vgamut matrix cast, BMD WG removed - all resolved by name in the
# bake, none touch the E-Gamut2/T-LogE path. Original tag-day asset:
# 75f73562c4b93ea8e9827939a34b4232f169e73816a10261bfe208863b152026.
sha256sum -c --quiet - <<'HASHES'
645ba89745691742c5403f8de5964b5934cd1c59bf06fb1f1a1f6b45f54830a1  external/dctl/OpenDRT_v1.1.0.dctl
29311a6c6d0a52ed818214a880296e0ccd8919f318e517b9470485c82eebcbaa  external/openDRT_v1.0.0_full-config_v2.zip
f10891fc7d34e78e71b535e3f92b3c612177320f990641283247cb84bb8d62ad  external/blender-5.2/config.ocio
3092cb033649723e499b03d38d7de5a0995361d3fbc0c52bd8c8af3c942d32fe  external/dctl/OpenDRT_v1.1.0.nk
HASHES
echo "external/: all pins verified."