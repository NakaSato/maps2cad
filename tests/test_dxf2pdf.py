"""Plot colour policy (scripts/dxf2pdf.py).

The sheet's frame, title block, north arrow and crop rectangle are all on
ACI 7. Under the wrong policy that renders white on white paper: the plot
still looks plausible — the coloured linework is all there — and is missing
the half a reviewer reads.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

ezdxf = pytest.importorskip("ezdxf")
pytest.importorskip("matplotlib")

spec = importlib.util.spec_from_file_location("dxf2pdf", SCRIPTS / "dxf2pdf.py")
dxf2pdf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dxf2pdf)


def test_colour_plots_never_use_the_swap_policy():
    """COLOR_SWAP_BW is a swap, not a "make white ink printable" switch."""
    from ezdxf.addons.drawing.config import ColorPolicy

    assert dxf2pdf.plot_config(True).color_policy is ColorPolicy.COLOR
    assert dxf2pdf.plot_config(False).color_policy is ColorPolicy.BLACK


def test_an_aci_7_line_is_visible_ink_on_a_colour_plot(tmp_path):
    """Rendered, not reasoned about: draw one ACI 7 line and check what
    colour actually reaches the canvas."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    doc = ezdxf.new("R2010")
    doc.layers.add("C-ANNO-TTLB", color=7)          # the title-block layer
    doc.modelspace().add_line((0, 0), (100, 0),
                              dxfattribs={"layer": "C-ANNO-TTLB"})

    fig = plt.figure()
    ax = fig.add_axes([0, 0, 1, 1])
    Frontend(RenderContext(doc), MatplotlibBackend(ax),
             config=dxf2pdf.plot_config(True)).draw_layout(doc.modelspace())

    drawn = ax.collections + ax.lines
    assert drawn, "nothing reached the canvas"
    colours = []
    for artist in drawn:
        value = (artist.get_colors() if hasattr(artist, "get_colors")
                 else [artist.get_color()])
        colours.extend(matplotlib.colors.to_rgb(c) for c in value)
    assert colours, "no colour resolved"
    # White on white paper is invisible ink; anything darker is fine.
    assert all(sum(c) < 2.9 for c in colours), colours
    plt.close(fig)
