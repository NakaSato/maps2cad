# /// script
# requires-python = ">=3.11"
# dependencies = ["ezdxf[draw]"]
# ///
"""Convert a DXF drawing to a print-ready PDF."""
import argparse
from pathlib import Path

import ezdxf
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
from ezdxf.addons.drawing.config import Configuration, BackgroundPolicy, ColorPolicy
import matplotlib.pyplot as plt

PAPER_INCHES = {  # landscape (w, h)
    "A4": (11.69, 8.27),
    "A3": (16.54, 11.69),
    "A2": (23.39, 16.54),
    "A1": (33.11, 23.39),
    "A0": (46.81, 33.11),
}


def plot_config(color: bool) -> Configuration:
    """How linework is coloured on paper.

    ColorPolicy.COLOR keeps the layer colours and lets ezdxf resolve ACI 7
    against the background, which on white paper is black.

    NOT COLOR_SWAP_BW, which was here and is a swap: it turned every ACI 7
    entity *white*, and on a white background that is invisible ink. The
    whole title block, sheet frame, north arrow and crop rectangle vanished
    from every coloured plot — 0 dark pixels in the title-block strip
    against 1019 with this policy — while the coloured linework rendered
    fine, so the plot looked plausible and was missing the half a reviewer
    reads. serve.py plots previews in colour by default, so every sheet the
    web app showed had no title block.
    """
    return Configuration(
        background_policy=BackgroundPolicy.WHITE,
        color_policy=ColorPolicy.COLOR if color else ColorPolicy.BLACK,
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("dxf", help="input DXF file")
    p.add_argument("-o", "--out", help="output PDF (default: <input>.pdf)")
    p.add_argument("--size", default="A3", choices=PAPER_INCHES, help="paper size (landscape)")
    p.add_argument("--color", action="store_true",
                   help="keep layer colors (default: all-black linework)")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--layout", metavar="NAME",
                   help="Plot a paper-space layout (e.g. SHEET) instead of "
                        "model space — this is the sheet with the title block")
    args = p.parse_args()

    out = Path(args.out) if args.out else Path(args.dxf).with_suffix(".pdf")
    doc = ezdxf.readfile(args.dxf)

    config = plot_config(args.color)

    if args.layout:
        names = [l for l in doc.layout_names() if l != "Model"]
        if args.layout not in names:
            p.error(f"no layout named {args.layout!r}; this file has: "
                    + (", ".join(names) if names else "none (model space only)"))
        target = doc.layouts.get(args.layout)
    else:
        target = doc.modelspace()

    w, h = PAPER_INCHES[args.size]
    fig = plt.figure(figsize=(w, h))
    ax = fig.add_axes([0.02, 0.02, 0.96, 0.96])
    Frontend(RenderContext(doc), MatplotlibBackend(ax),
             config=config).draw_layout(target)
    fig.savefig(out, dpi=args.dpi, facecolor="white")
    print(f"Saved: {out} ({args.size} landscape, {args.dpi} dpi, "
          f"{'color' if args.color else 'black'} linework"
          + (f", layout '{args.layout}'" if args.layout else "") + ")")


if __name__ == "__main__":
    main()
