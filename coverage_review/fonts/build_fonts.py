"""Rebuild the static Korean TTFs ReportLab embeds (it cannot use variable fonts or woff2).

Source: Noto Sans KR variable font (SIL OFL 1.1, see OFL.txt), e.g. C:\\Windows\\Fonts\\NotoSansKR-VF.ttf.
Instances wght=400 / wght=700, subset to the same ranges as NotoSansKR-subset.woff2.

  python coverage_review/fonts/build_fonts.py [path/to/NotoSansKR-VF.ttf]
"""

from __future__ import annotations

import pathlib
import sys

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = pathlib.Path(__file__).resolve().parent
RANGES = ("U+0020-007E,U+00A0-024F,U+2000-23FF,U+2460-27BF,U+3000-303F,U+3130-318F,U+3200-32FF,"
          "U+AC00-D7A3,U+FF00-FFEF")


def unicodes() -> list[int]:
    out = []
    for part in RANGES.split(","):
        a, b = part[2:].split("-")
        out += range(int(a, 16), int(b, 16) + 1)
    return out


def build(src: str, wght: int, dst: pathlib.Path) -> None:
    font = instancer.instantiateVariableFont(TTFont(src), {"wght": wght}, updateFontNames=False)
    opts = subset.Options()
    opts.layout_features = ["*"]
    opts.name_IDs = ["*"]
    opts.notdef_outline = True
    opts.hinting = False  # smaller; PDFs don't use TrueType hints
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=unicodes())
    sub.subset(font)
    style = "Bold" if wght >= 700 else "Regular"
    name = font["name"]
    name.removeNames(nameID=16)  # typographic family/subfamily of the variable font
    name.removeNames(nameID=17)
    for nid, val in {1: "Noto Sans KR", 2: style, 3: f"NotoSansKR-{style};static-subset",
                     4: f"Noto Sans KR {style}", 6: f"NotoSansKR-{style}"}.items():
        name.setName(val, nid, 3, 1, 0x409)
        name.setName(val, nid, 1, 0, 0)
    font["OS/2"].usWeightClass = wght
    bold = wght >= 700
    font["OS/2"].fsSelection = (font["OS/2"].fsSelection & ~0b1100001) | (0b100000 if bold else 0b1000000)
    font["head"].macStyle = 1 if bold else 0
    font.save(dst)
    print(dst.name, dst.stat().st_size)


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else r"C:\Windows\Fonts\NotoSansKR-VF.ttf"
    build(src, 400, HERE / "NotoSansKR-Regular.ttf")
    build(src, 700, HERE / "NotoSansKR-Bold.ttf")
