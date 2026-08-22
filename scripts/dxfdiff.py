#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "ezdxf",
# ]
# ///
"""Compare two DXF files entity-by-entity and label-by-label.

The repo has two ways to reach a drawing — topo2cad.py draws during
extraction, db2dxf.py draws from the SQLite staging layer — and they are
required to agree. This is the check:

    uv run scripts/topo2cad.py --lat 13.746 --lon 100.534 \\
        --dem dem/dem_n13_e100.tif --out a.dxf --db s.sqlite --project x
    uv run scripts/db2dxf.py --db s.sqlite --project 1 --out b.dxf
    uv run scripts/dxfdiff.py a.dxf b.dxf

Exit status is 0 when the drawings match and 1 when they do not, so it works
as a regression gate. Counting entities per layer is not enough on its own:
a label can be present in both drawings and still sit 287 m away, which is
what --positions catches (it is on by default; --no-positions skips it).
"""

from __future__ import annotations

import argparse
import collections
import math
import sys


def survey(path):
    """(entity counts, MTEXT positions, text styles, layer table, XDATA)."""
    import ezdxf

    doc = ezdxf.readfile(path)
    counts = collections.Counter()
    labels = collections.defaultdict(list)
    # Extended data is the drawing's provenance: which source each entity
    # came from, under which application id, with which tags. Nothing
    # compared it before, so a re-issue could hand back a drawing stripped
    # of its source data — or file a survey's columns under OSM — and this
    # tool still said IDENTICAL. It is a multiset keyed on the content,
    # because the two routes draw in different orders.
    #
    # Every registered appid is compared, ezdxf's and AutoCAD's included:
    # the background mask on an MTEXT rides in ACAD XDATA, and a label that
    # lost its mask is a real difference that also went unseen here.
    appids = [ap.dxf.name for ap in doc.appids]
    xdata = collections.Counter()
    for e in doc.modelspace():
        counts[(e.dxftype(), e.dxf.layer)] += 1
        if e.dxftype() == "MTEXT":
            labels[(e.dxf.layer, e.text)].append(
                (e.dxf.insert.x, e.dxf.insert.y, e.dxf.rotation))
        for appid in appids:
            if not e.has_xdata(appid):
                continue
            tags = tuple((code, str(value))
                         for code, value in e.get_xdata(appid))
            xdata[(appid, tags)] += 1
    styles = {s.dxf.name: getattr(s.dxf, "font", "") for s in doc.styles}
    # The layer table carries colour, linetype and lineweight — a CENTER
    # centreline, a DASHED crop line, a monochrome sheet. Two drawings can
    # hold identical geometry and still plot differently, so compare it.
    layers = {ly.dxf.name: (ly.dxf.color, ly.dxf.linetype, ly.dxf.lineweight)
              for ly in doc.layers}
    return counts, labels, styles, layers, xdata


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("a", help="first DXF (conventionally topo2cad.py output)")
    p.add_argument("b", help="second DXF (conventionally db2dxf.py output)")
    p.add_argument("--tolerance", type=float, default=0.01, metavar="M",
                   help="metres of label drift to accept (default 0.01)")
    p.add_argument("--no-positions", action="store_true",
                   help="compare counts only, not where labels landed")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="print only the verdict")
    a = p.parse_args()

    ca, la, sa, ya, xa = survey(a.a)
    cb, lb, sb, yb, xb = survey(a.b)
    problems = 0

    if not a.quiet:
        print(f"{'entity':<12}{'layer':<18}{'A':>7}{'B':>7}")
    for key in sorted(set(ca) | set(cb)):
        x, y = ca.get(key, 0), cb.get(key, 0)
        if x != y:
            problems += 1
        if not a.quiet:
            flag = "" if x == y else "   <-- DIFFERS"
            print(f"  {key[0]:<10}{key[1]:<18}{x:>7}{y:>7}{flag}")

    only_a = sorted(k[1] for k in set(la) - set(lb))
    only_b = sorted(k[1] for k in set(lb) - set(la))
    problems += len(only_a) + len(only_b)
    if only_a:
        print(f"\nlabels only in {a.a}: {only_a}")
    if only_b:
        print(f"labels only in {a.b}: {only_b}")

    compared = worst = rot_bad = 0
    worst_label = None
    if not a.no_positions:
        for key in set(la) & set(lb):
            if len(la[key]) != len(lb[key]):
                continue        # already counted as an entity difference
            for (x1, y1, r1), (x2, y2, r2) in zip(sorted(la[key]),
                                                  sorted(lb[key])):
                compared += 1
                drift = math.hypot(x1 - x2, y1 - y2)
                if drift > worst:
                    worst, worst_label = drift, key[1]
                if drift > a.tolerance:
                    problems += 1
                # rotations are degrees; compare on the shorter arc
                if abs(((r1 - r2 + 180) % 360) - 180) > 0.01:
                    rot_bad += 1
                    problems += 1

    print()
    if compared:
        print(f"labels compared        : {compared}")
        print(f"worst drift            : {worst:.4f} m"
              + (f"  ({worst_label!r})" if worst > a.tolerance else ""))
        print(f"rotation mismatches    : {rot_bad}")
    missing_styles = sorted(set(sa) - set(sb)) + sorted(set(sb) - set(sa))
    if missing_styles:
        print(f"text styles in one only : {missing_styles}")
        problems += len(missing_styles)

    only_layers = sorted(set(ya) ^ set(yb))
    if only_layers:
        print(f"layers in one only      : {only_layers}")
        problems += len(only_layers)
    differing = [(n, ya[n], yb[n]) for n in sorted(set(ya) & set(yb))
                 if ya[n] != yb[n]]
    if differing:
        print("layer properties differ (colour, linetype, lineweight):")
        for name, x, y in differing:
            print(f"    {name:<18} {x}  vs  {y}")
        problems += len(differing)

    # ---- extended data ------------------------------------------------
    def by_appid(counter):
        out = collections.Counter()
        for (appid, _tags), n in counter.items():
            out[appid] += n
        return out

    ta, tb = by_appid(xa), by_appid(xb)
    if not a.quiet and (ta or tb):
        print()
        print(f"{'XDATA appid':<24}{'A':>7}{'B':>7}")
        for appid in sorted(set(ta) | set(tb)):
            x, y = ta.get(appid, 0), tb.get(appid, 0)
            flag = "" if x == y else "   <-- DIFFERS"
            print(f"  {appid:<22}{x:>7}{y:>7}{flag}")
    # Diff the content, not just the totals: the same number of entities
    # can carry the wrong tags, or the right tags under the wrong appid.
    xdiff = (xa - xb) + (xb - xa)
    if xdiff:
        problems += sum(xdiff.values())
        print("\nXDATA differs on "
              f"{sum(xdiff.values())} entity/entities:")
        for (appid, tags), n in sorted(xdiff.items())[:5]:
            where = "A" if xa[(appid, tags)] else "B"
            sample = ", ".join(v for _c, v in tags[:3])
            print(f"    {n} x [{appid}] {where} only: {sample}"
                  + (" ..." if len(tags) > 3 else ""))
        if len(xdiff) > 5:
            print(f"    ... and {len(xdiff) - 5} more")

    if problems:
        print(f"\nDIFFER — {problems} problem(s)")
        return 1
    print("\nIDENTICAL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
