#!/usr/bin/env python3
"""HTML for the maps2cad web app.

Everything the browser sees lives here: the stylesheet, the page shell and
one builder per page. It is deliberately one direction of dependency —
`serve.py` imports this, never the reverse — so nothing in here reaches for
a database, the filesystem or a subprocess. A builder is handed the data it
renders and returns bytes, which is what makes a page testable without a
server and what keeps `serve.py` about routing rather than markup.

Stdlib only, like `serve.py`: no template engine, no build step, no
third-party CSS. The app has to run anywhere Python does.
"""

from __future__ import annotations

import base64
import html
import json
import urllib.parse
from pathlib import Path

CSS = """
:root{--paper:#FBFAF8;--sheet:#fff;--ink:#102A43;--soft:#486174;
--rule:#C9D6DF;--faint:#E4EBF0;--survey:#C45A00;--marker:#D90429;--teal:#00727C;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--paper:#0E1720;--sheet:#16222E;--ink:#E6EEF4;--soft:#9DB2C2;--rule:#2E4256;
--faint:#223140;--survey:#F08A2E;--marker:#FF5A6E;--teal:#38B0B9;}}
:root[data-theme="dark"]{--paper:#0E1720;--sheet:#16222E;--ink:#E6EEF4;
--soft:#9DB2C2;--rule:#2E4256;--faint:#223140;--survey:#F08A2E;
--marker:#FF5A6E;--teal:#38B0B9;}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-size:16px;
line-height:1.6;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",
Roboto,sans-serif;-webkit-font-smoothing:antialiased}
/* Full width: the page fills the window rather than sitting in a 1080px
   column. What benefits is everything with columns of its own — the
   building table, the history, the file cards, the form grids, the map
   preview. Running text does not benefit, so the prose rules below keep
   their own measure in ch: a 34-inch monitor should widen the tables, not
   stretch a sentence across two feet of glass. */
.wrap{max-width:none;margin:0;padding:clamp(14px,2vw,28px)}
.frame{border:1.5px solid var(--ink);background:var(--sheet);
padding:clamp(18px,2.4vw,40px);min-height:calc(100vh - clamp(28px,4vw,56px))}
.eyebrow{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;
letter-spacing:.16em;text-transform:uppercase;color:var(--soft);margin:0 0 10px}
h1{font-size:clamp(25px,3.2vw,40px);letter-spacing:-.02em;margin:0 0 8px;
line-height:1.1;max-width:24ch}
.lede{color:var(--soft);margin:0 0 26px;max-width:62ch}
label{display:block;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;
color:var(--soft);margin:0 0 5px}
/* Full-bleed suits tables, cards and the map preview. A single text field
   is the one thing it does not suit: a 900px box for "Width (m)" is harder
   to aim at, not easier. The cap lets a field stop growing while its row
   keeps the width. */
input[type=text],input[type=number],select{width:100%;max-width:34rem;
padding:9px 11px;border:1px solid var(--rule);background:var(--paper);
color:var(--ink);font-size:15px;font-family:inherit;border-radius:0}
input:focus,select:focus{outline:2px solid var(--survey);outline-offset:1px}
.grid{display:grid;gap:16px}
.g2{grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
fieldset{border:1px solid var(--rule);padding:18px;margin:22px 0 0}
legend{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;
letter-spacing:.14em;text-transform:uppercase;color:var(--survey);padding:0 8px}
.check{display:flex;align-items:center;gap:9px;font-size:14.5px;color:var(--ink)}
.check input{width:16px;height:16px;accent-color:var(--survey)}
.checks{display:grid;gap:10px 18px;
grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
button{margin-top:22px;padding:12px 24px;border:1.5px solid var(--ink);
background:var(--ink);color:var(--paper);font-size:15px;font-weight:600;
cursor:pointer;font-family:inherit}
button:hover{background:var(--survey);border-color:var(--survey)}
button:focus-visible{outline:2px solid var(--survey);outline-offset:2px}
a{color:var(--survey)}
.err{border-left:3px solid var(--marker);background:var(--faint);
padding:12px 16px;margin:0 0 22px;white-space:pre-wrap;font-size:14.5px}
figure{margin:24px 0 0;border:1px solid var(--rule)}
figure img{display:block;width:100%;height:auto}
.files{display:flex;gap:10px;flex-wrap:wrap;margin-top:20px}
.card{display:inline-flex;border:1px solid var(--rule);align-items:stretch}
.card:hover{border-color:var(--survey)}
.card>a{display:inline-block;padding:9px 15px;text-decoration:none;
color:var(--ink);font-size:14px}
.card>a:hover{color:var(--survey)}
.card a b{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;
letter-spacing:.1em;color:var(--soft);display:block;font-weight:400}
.card .dlicon{border-left:1px solid var(--rule);display:flex;align-items:center;
padding:0 12px;font-size:16px;color:var(--soft)}
.card .dlicon:hover{color:var(--survey);background:var(--faint)}
/* The CAD export is what most runs are for, so it reads as the one action
   on the page rather than as one tile among five. */
.card.primary{border-color:var(--survey);border-width:1.5px}
.card.primary>a{color:var(--survey);font-weight:600}
.card.primary a b{color:var(--survey);opacity:.75;font-weight:400}
.card.primary>a:hover{background:var(--faint)}
.card.primary .dlicon{border-left-color:var(--survey)}
.files a:focus-visible,.hist a:focus-visible{outline:2px solid var(--survey);
outline-offset:1px}
dl.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
gap:1px;background:var(--rule);border:1px solid var(--rule);margin:22px 0 0}
dl.meta>div{background:var(--sheet);padding:13px 15px}
dl.meta dt{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10px;
letter-spacing:.12em;text-transform:uppercase;color:var(--soft);margin:0 0 4px}
dl.meta dd{margin:0;font-size:15px;font-variant-numeric:tabular-nums}
pre.log{background:var(--faint);border:1px solid var(--rule);padding:14px 16px;
overflow-x:auto;font-size:12.5px;line-height:1.65;margin:22px 0 0;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap}
.back{display:inline-block;margin-top:26px;font-size:14.5px}
/* Generating takes 18-105 s depending on whether the DEM tile is cached,
   so the wait needs a real indicator, not a disabled button. */
#busy{display:none;margin-top:20px}
#busy.on{display:block}
.load{border:1px solid var(--rule);border-left:3px solid var(--survey);
padding:14px 16px;background:var(--faint)}
.load-head{display:flex;justify-content:space-between;align-items:baseline;
gap:12px}
.load-title{font-size:14.5px;color:var(--ink);font-weight:600}
.load-time{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:20px;color:var(--survey);font-variant-numeric:tabular-nums;
letter-spacing:.02em}
.load-note{font-size:13px;color:var(--soft);margin-top:6px}
/* Indeterminate: the server sends nothing until it is finished, so a
   percentage would be invented. A sweep says "working" without lying. */
.load-bar{position:relative;height:3px;background:var(--rule);margin-top:12px;
overflow:hidden}
.load-bar::after{content:"";position:absolute;inset:0 auto 0 0;width:40%;
background:var(--survey);animation:sweep 1.6s cubic-bezier(.4,0,.2,1) infinite}
@keyframes sweep{0%{left:-40%}100%{left:100%}}
.load.over .load-time{color:var(--marker)}
@media (prefers-reduced-motion:reduce){
.load-bar::after{animation:none;width:100%;opacity:.4}}
footer{margin-top:34px;padding-top:14px;border-top:1px solid var(--rule);
font-size:12.5px;color:var(--soft);font-family:ui-monospace,SFMono-Regular,
Menlo,monospace}
h2{font-size:15px;letter-spacing:-.01em;margin:34px 0 12px;padding-bottom:9px;
border-bottom:1px solid var(--rule);display:flex;justify-content:space-between;
align-items:baseline;gap:12px}
h2 a{font-size:12.5px;text-decoration:none}
.hist{width:100%;border-collapse:collapse;font-size:13.5px}
.hist th{text-align:left;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--soft);
font-weight:400;padding:0 12px 7px 0;border-bottom:1px solid var(--rule)}
.hist td{padding:9px 12px 9px 0;border-bottom:1px solid var(--faint);
vertical-align:top}
.hist td.num{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:12.5px;font-variant-numeric:tabular-nums;white-space:nowrap}
.hist td.dl{white-space:nowrap}
.hist td.proj{font-size:12.5px;overflow-wrap:anywhere;max-width:26ch}
/* The CAD file is the deliverable; make it obvious which link hands it over */
.hist a.dl-dxf{font-weight:600;color:var(--survey)}
.hist td.dl a{display:inline-block;font-family:ui-monospace,SFMono-Regular,
Menlo,monospace;font-size:10.5px;letter-spacing:.06em;padding:2px 7px;
margin:0 3px 3px 0;border:1px solid var(--rule);text-decoration:none;
color:var(--ink)}
.hist td.dl a:hover{border-color:var(--survey);color:var(--survey)}
.wide{overflow-x:auto}
.hist input[type=text]{width:100%;max-width:none;min-width:190px;padding:5px 8px;font-size:13px}
.ok{border-left:3px solid var(--teal);background:var(--faint);padding:11px 15px;
margin:0 0 20px;font-size:14.5px}
.filters{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 12px}
.filters input[type=text]{flex:1 1 260px;min-width:200px;max-width:44rem;padding:8px 11px}
.filters select{width:auto;min-width:150px;padding:8px 11px}
.filters button{margin-top:0;padding:8px 18px;font-size:14px}
.filters .clear{font-size:13.5px;text-decoration:none}
.pager{display:flex;gap:6px;flex-wrap:wrap;margin-top:16px;align-items:center}
.pg{display:inline-block;padding:6px 11px;border:1px solid var(--rule);
text-decoration:none;color:var(--ink);font-size:13.5px;
font-variant-numeric:tabular-nums}
.pg:hover{border-color:var(--survey);color:var(--survey)}
.pg.on{background:var(--ink);color:var(--paper);border-color:var(--ink)}
.pg.off{color:var(--soft);border-color:transparent}
/* .note was used in the markup from the start and never had a rule, so
   every aside rendered at body size and read as heavily as the lede. */
.note{color:var(--soft);font-size:13.5px;line-height:1.6;margin:10px 0 0;
max-width:76ch}
.note code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:12.5px;background:var(--faint);padding:1px 5px}
/* The form asks for eleven things. Grouping them means a first-time user
   reads three questions rather than a wall of controls. */
.sect{margin:0 0 26px}
.sect+.sect{padding-top:24px;border-top:1px solid var(--faint)}
.sect-h{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;
letter-spacing:.14em;text-transform:uppercase;color:var(--survey);
margin:0 0 14px;font-weight:400}
details.adv{border:1px solid var(--rule);margin:0 0 26px}
details.adv>summary{padding:12px 16px;cursor:pointer;font-size:14.5px;
color:var(--ink);list-style:none;display:flex;justify-content:space-between;
align-items:center;gap:12px}
details.adv>summary::-webkit-details-marker{display:none}
details.adv>summary::after{content:"+";font-family:ui-monospace,monospace;
color:var(--soft);font-size:17px;line-height:1}
details.adv[open]>summary::after{content:"−"}
details.adv>summary:hover{background:var(--faint)}
details.adv>summary:focus-visible{outline:2px solid var(--survey);
outline-offset:-2px}
details.adv .adv-body{padding:0 16px 18px;border-top:1px solid var(--faint)}
details.adv .adv-body>*:first-child{margin-top:16px}
/* The extent and the sheet are chosen independently, and the pairing decides
   the plot scale — 1000 x 750 on A3 is 1:5000, a locality map, which is not
   obvious from either field on its own. So the form says so as you type. */
.readout{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;max-width:80ch;
margin:14px 0 0;padding:11px 14px;background:var(--faint);
border-left:3px solid var(--teal);font-size:13.5px;color:var(--soft)}
.readout b{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:15px;color:var(--ink);font-variant-numeric:tabular-nums;
font-weight:600;letter-spacing:.01em}
.readout.warn{border-left-color:var(--survey)}
.readout.over{border-left-color:var(--marker)}
/* Step 1 is one field. The location button sits beside it rather than
   under it, so the row still reads as a single question. */
.locrow{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}
.locfield{flex:1 1 420px;min-width:260px;max-width:44rem}
.locfield input{max-width:none}
button.ghost{margin-top:0;padding:9px 16px;background:transparent;
color:var(--ink);border:1px solid var(--rule);font-weight:400;font-size:14px;
white-space:nowrap}
button.ghost:hover{border-color:var(--survey);color:var(--survey);
background:transparent}
button.ghost:disabled{opacity:.55;cursor:default;border-color:var(--rule);
color:var(--soft)}
/* Three presets instead of two number fields nobody can price. Each says
   what it is for and what it plots at, because the extent alone does not
   tell you which of those you are asking for. */
.presets{display:grid;gap:12px;
grid-template-columns:repeat(auto-fit,minmax(230px,1fr));max-width:96rem}
button.preset{margin:0;display:flex;flex-direction:column;gap:3px;
align-items:flex-start;text-align:left;padding:13px 15px;
background:var(--sheet);color:var(--ink);border:1px solid var(--rule);
font-weight:400;cursor:pointer}
button.preset:hover{border-color:var(--survey);background:var(--faint)}
button.preset.on{border-color:var(--survey);border-width:1.5px;
background:var(--faint)}
.preset-name{font-size:15px;font-weight:600}
button.preset.on .preset-name{color:var(--survey)}
.preset-size{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:12.5px;color:var(--soft);font-variant-numeric:tabular-nums}
.preset-why{font-size:12.5px;color:var(--soft);line-height:1.45}
.extent{display:flex;gap:12px;flex-wrap:wrap;margin-top:14px}
.extent>div{flex:0 1 170px}
/* The run watcher. The plan is shown whole from the start — a step still to
   come is as much information as the one running. */
ol.steps{list-style:none;margin:24px 0 0;padding:0;max-width:76rem}
ol.steps li{display:grid;grid-template-columns:26px minmax(150px,auto) 1fr;
gap:12px;align-items:baseline;padding:13px 0;
border-bottom:1px solid var(--faint)}
ol.steps li:first-child{border-top:1px solid var(--faint)}
.step-mark{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:15px;color:var(--rule);text-align:center}
.step-name{font-size:15px;color:var(--soft)}
.step-detail{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:12.5px;color:var(--soft);overflow-wrap:anywhere}
li.running .step-mark,li.running .step-name{color:var(--survey)}
li.running .step-name{font-weight:600}
li.done .step-mark{color:var(--teal)}
li.done .step-name{color:var(--ink)}
li.failed .step-mark,li.failed .step-name{color:var(--marker)}
li.skipped .step-name,li.skipped .step-detail{color:var(--soft);opacity:.7}
/* Indeterminate on purpose: the server cannot say what fraction of a
   contour trace is done, and a percentage it invented would be a lie. */
.runbar{position:relative;height:3px;background:var(--rule);margin-top:22px;
overflow:hidden;max-width:76rem}
.runbar::after{content:"";position:absolute;inset:0 auto 0 0;width:38%;
background:var(--survey);animation:sweep 1.6s cubic-bezier(.4,0,.2,1) infinite}
@media (prefers-reduced-motion:reduce){
.runbar::after{animation:none;width:100%;opacity:.4}}
.runfoot{display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;
margin-top:12px;font-size:13.5px;color:var(--soft);max-width:76rem}
.runfoot b{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-variant-numeric:tabular-nums;color:var(--ink);font-weight:600}
/* Steps on the left, whatever the run has drawn so far on the right. The
   preview column sticks so it stays in view while the step list grows. */
.runwrap{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,22rem);
gap:28px;align-items:start;margin-top:24px}
.runcol{min-width:0}
.runcol ol.steps{margin-top:0}
.runprev{min-width:0;position:sticky;top:18px;border:1px solid var(--rule);
border-radius:10px;overflow:hidden;background:var(--card,transparent)}
.prevhead{display:flex;align-items:center;justify-content:space-between;
gap:10px;flex-wrap:wrap;padding:10px 12px;border-bottom:1px solid var(--rule);
font-size:13.5px}
.prevtabs{display:flex;gap:6px;flex-wrap:wrap}
.prevtabs button{font:inherit;font-size:12px;padding:3px 8px;cursor:pointer;
border:1px solid var(--rule);border-radius:999px;background:transparent;
color:var(--soft)}
.prevtabs button.on{color:var(--ink);border-color:var(--ink)}
.prevbody{display:flex;align-items:center;justify-content:center;
min-height:15rem;padding:10px;background:#fff}
.prevbody img{max-width:100%;height:auto;display:block}
.prevbody iframe{width:100%;height:26rem;border:0;display:block}
.prevwait{color:var(--soft);font-size:13.5px;margin:0;padding:18px 6px;
text-align:center;line-height:1.5}
.prevnote{margin:0;padding:9px 12px;border-top:1px solid var(--rule);
font-size:12.5px}
@media (max-width:900px){
.runwrap{grid-template-columns:minmax(0,1fr)}
.runprev{position:static;order:-1}
.prevbody iframe{height:18rem}}
"""


# The plot-scale readout on the form needs the same arithmetic sheet.py uses,
# and serve.py cannot import sheet.py: that module needs ezdxf, and this app
# staying stdlib-only is what lets it run anywhere Python does. So the three
# tables are restated here and a test compares them against sheet.py's, the
# same way the layer tables are kept in step across the writers.
SHEET_MM = {"A4": (297, 210), "A3": (420, 297), "A2": (594, 420),
            "A1": (841, 594), "A0": (1189, 841)}
BLOCK_W = {"A4": 80, "A3": 100, "A2": 120, "A1": 140, "A0": 160}
ROUND_SCALES = [200, 250, 500, 1000, 1250, 2000, 2500, 5000, 10000, 20000]


def viewport_mm(size: str):
    """Usable viewport of a sheet, after the margins and the title block."""
    pw, ph = SHEET_MM[size.upper()]
    margin = 10
    return (pw - margin - BLOCK_W[size.upper()] - margin * 2,
            ph - margin * 2 - 4)


def fitting_scale(width_m: float, height_m: float, size: str):
    """Smallest round scale at which the extent fits, or None if none does.

    The browser runs this same rule on the form. It is written once here and
    the *result* — the viewport in millimetres — is handed to the page, so
    the only thing the script repeats is the loop, not the arithmetic.
    """
    vp_w, vp_h = viewport_mm(size)
    for s in ROUND_SCALES:
        if width_m * 1000 / s <= vp_w and height_m * 1000 / s <= vp_h:
            return s
    return None


# Vue 3, global build, straight from a CDN — no Node, no npm, no build step,
# which is the only way a front-end framework belongs in a repo whose whole
# premise is that it runs anywhere Python does.
#
# Two consequences to know rather than discover. The page reaches a third
# party at load: where unpkg is slow or blocked the interactive parts do not
# start, so the pages that matter most are still plain server-rendered HTML
# and forms, and Vue is used only where it earns its place — the generate
# form and the run watcher. And the templates below use `v-text` and
# `v-bind` rather than `{{ }}` interpolation, because these pages are built
# in Python f-strings where a literal brace has to be doubled; directives
# keep the markup readable instead of a thicket of `{{{{`.
#
# To drop the CDN entirely: save vue.global.js beside this file, serve it,
# and point VUE_SRC at it. Nothing else changes.
VUE_SRC = "https://unpkg.com/vue@3/dist/vue.global.js"


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style>
<script src="{VUE_SRC}"></script></head>
<body><div class="wrap"><div class="frame">{body}</div></div>
<script>
// Any form holding a #busy panel shows it on submit and runs a countdown
// from that form's estimate. The server streams nothing back until the
// whole job is done, so this is a timer, not progress — when the estimate
// runs out it says so and counts up rather than sitting at zero pretending.
(function(){{
  function pad(n){{return (n<10?'0':'')+n;}}
  function clock(s){{var m=Math.floor(Math.abs(s)/60);return (s<0?'+':'')+m+':'+pad(Math.abs(s)%60);}}
  document.querySelectorAll('form').forEach(function(form){{
    var busy = form.querySelector('#busy'); if(!busy) return;
    form.addEventListener('submit', function(){{
      var btn = form.querySelector('button[type=submit]') || form.querySelector('button');
      if(btn){{ btn.disabled = true; btn.textContent = btn.dataset.busy || 'Working…'; }}
      busy.classList.add('on');
      var panel = busy.querySelector('.load');
      var est = parseInt(panel && panel.dataset.estimate || '45', 10);
      var timeEl = busy.querySelector('.load-time');
      var noteEl = busy.querySelector('.load-over');
      var left = est, elapsed = 0;
      if(timeEl) timeEl.textContent = clock(left);
      setInterval(function(){{
        elapsed++; left--;
        if(timeEl) timeEl.textContent = clock(left);
        if(left < 0 && panel && !panel.classList.contains('over')){{
          panel.classList.add('over');
          if(noteEl) noteEl.style.display = 'block';
        }}
      }}, 1000);
    }});
  }});
  // Coming back via the back button must not leave a dead spinner up
  window.addEventListener('pageshow', function(){{
    document.querySelectorAll('#busy').forEach(function(b){{
      b.classList.remove('on');
      var p = b.querySelector('.load'); if(p) p.classList.remove('over');
    }});
    document.querySelectorAll('button[data-idle]').forEach(function(b){{
      b.disabled = false; b.textContent = b.dataset.idle;
    }});
  }});
}})();
</script>
</body></html>
""".encode()


def history_table(rows: list[dict], projects: dict | None = None) -> str:
    """One row per run, with the project it staged beside its files.

    A CAD run leaves two things behind — the files in its own folder and a
    staged project that can be renamed and re-issued — and they used to be
    listed on two pages with no link between them.
    """
    projects = projects or {}
    if not rows:
        return ('<p class="note">No maps generated yet — your runs will be '
                "listed here.</p>")
    out = ['<table class="hist"><thead><tr><th>When</th><th>Location</th>'
           '<th>Area</th><th>Export</th><th>Files</th><th>Project</th>'
           '</tr></thead><tbody>']
    for r in rows:
        p = r["params"]
        # DXF downloads rather than previews: it has no browser viewer, so
        # /view substitutes the plot PDF, and clicking "DXF" to be handed a
        # PDF is not what the link says it does. The rest do preview.
        links = " ".join(
            (f'<a class="dl-dxf" href="/file/{r["id"]}/dxf" download '
             f'title="Download the DXF">⤓ DXF</a>' if k == "dxf" else
             f'<a href="/view/{r["id"]}/{k}" target="_blank" rel="noopener" '
             f'title="Preview {k.upper()}">{k.upper()}</a>')
            for k in ("dxf", "plot", "pdf", "png", "csv") if r.get(k))
        name = r.get("project") or ""
        pid = projects.get(name)
        project = (f'<a href="/project/{pid}" title="Correct names and '
                   f're-issue">{html.escape(name)}</a>' if pid
                   else html.escape(name) or "—")
        out.append(
            f'<tr><td>{html.escape(r["when"])}</td>'
            f'<td class="num">{p["lat"]:.6f}, {p["lon"]:.6f}</td>'
            f'<td class="num">{p["width"]:.0f} × {p["height"]:.0f} m</td>'
            f'<td>{html.escape(p.get("export", "map"))}</td>'
            f'<td class="dl">{links or "—"}</td>'
            f'<td class="proj">{project}</td></tr>')
    out.append("</tbody></table>")
    return "".join(out)


# What the three preset buttons set, and what each is for. The scale shown
# is what that extent plots at on A3, the default sheet — which is the
# whole point of offering them: the pairing is what decides the scale, and
# picking an extent without knowing that is how a site plan comes out as a
# locality map.
PRESETS = [
    ("200", "150", "Site plan", "the parcel and its frontage", "1:1,000"),
    ("500", "400", "Site + context", "the block around it", "1:2,000"),
    ("1000", "750", "Locality", "where the site sits in the district",
     "1:5,000"),
]


def form_page(values: dict, error: str, recent: str,
              gov_fields, basemap_choices) -> bytes:
    v = values or {}
    g = v.get("gov", {})

    def val(key, default=""):
        return html.escape(str(v.get(key, default)))

    gov_inputs = "".join(
        f'<div><label for="{k}">{html.escape(lbl)}</label>'
        f'<input type="text" id="{k}" name="{k}" '
        f'value="{html.escape(str(g.get(k, "")))}"></div>'
        for k, lbl in gov_fields)

    err = f'<div class="err">{html.escape(error)}</div>' if error else ""
    sel = v.get("profile", "standard")
    sheet = v.get("sheet_size", "A3")

    def opts(names, chosen):
        return "".join(
            f'<option value="{n}"{" selected" if n == chosen else ""}>{n}'
            "</option>" for n in names)

    def opts_scale(values, chosen):
        return "".join(
            f'<option value="{n}"{" selected" if n == chosen else ""}>'
            f"1:{int(n):,}</option>" for n in values)

    basemap_opts = "".join(
        f'<option value="{k}"'
        f'{" selected" if v.get("basemap", "") == k else ""}>'
        f"{html.escape(label)}</option>"
        for k, label in basemap_choices.items())

    presets = "".join(
        f'<button type="button" class="preset" data-w="{w}" data-h="{h}">'
        f'<span class="preset-name">{html.escape(name)}</span>'
        f'<span class="preset-size">{w} × {h} m</span>'
        f'<span class="preset-why">{html.escape(why)} · {scale} on A3</span>'
        f'</button>' for w, h, name, why, scale in PRESETS)

    # The fold opens by itself when the run that bounced had set something
    # inside it, so an error never points at a control the page has hidden.
    advanced_used = any(v.get(k) for k in
                        ("cad_sheet", "cad_scale", "basemap", "sheet_size",
                         "title", "export")) or sel == "government"
    viewport_js = json.dumps({k: list(viewport_mm(k)) for k in SHEET_MM},
                             separators=(",", ":"))
    scales_js = json.dumps(ROUND_SCALES, separators=(",", ":"))
    return page("maps2cad", f"""
<p class="eyebrow">GPS coordinate → CAD drawing + site map</p>
<h1>maps2cad</h1>
<p class="lede">Give it a point on the ground. You get a DXF in true UTM
metres — double-line roads, contours, every building labelled — plus a
print-ready site map sheet and the inventory CSV that resolves the codes.</p>
{err}
<form method="post" action="/generate">
  <section class="sect">
    <h2 class="sect-h">1 · Where</h2>
    <div class="locrow">
      <div class="locfield">
        <label for="coords">Paste a coordinate, or a Google Maps link</label>
        <input type="text" id="coords" name="coords"
               placeholder="15.83384548, 104.39445555"
               value="{val('coords')}" autofocus autocomplete="off"
               spellcheck="false">
      </div>
      <button type="button" class="ghost" id="here">Use my location</button>
    </div>
    <p class="note" id="loc_note">Anything readable works: a decimal pair,
    <code>15°50'02"N 104°23'40"E</code>, a Google Maps or OpenStreetMap link,
    or the <code>geo:</code> link a phone shares.</p>
  </section>

  <section class="sect">
    <h2 class="sect-h">2 · How much ground</h2>
    <div class="presets">{presets}</div>
    <div class="extent">
      <div><label for="width">Width (m)</label>
        <input type="number" id="width" name="width" step="any" min="20"
               value="{val('width', 1000)}"></div>
      <div><label for="height">Height (m)</label>
        <input type="number" id="height" name="height" step="any" min="20"
               value="{val('height', 750)}"></div>
    </div>
    <p class="readout" id="readout"><b id="readout_scale">—</b>
      <span id="readout_note">Enter an extent to see the plot scale.</span></p>
  </section>

  <button type="submit" data-idle="Generate" data-busy="Generating…">
  Generate</button>
  <div id="busy"><div class="load" data-estimate="60">
    <div class="load-head"><span class="load-title">Generating…</span>
      <span class="load-time">1:00</span></div>
    <div class="load-bar"></div>
    <p class="load-note">Querying Overpass, reading the elevation tile,
    tracing contours and drawing. Measured runs take 18–105 s; the first
    export in a new 1°×1° square also downloads a ~40 MB elevation tile.</p>
    <p class="load-note load-over" style="display:none">Over the estimate —
    still working. A cold start or a fresh elevation tile adds a minute or
    so. Leave this tab open.</p>
  </div></div>

  <details class="adv" id="adv"{" open" if advanced_used else ""}>
    <summary>Everything else — output, layout, sheets, background map</summary>
    <div class="adv-body">
      <div class="grid g3">
        <div><label for="export">Export</label>
          <select id="export" name="export" onchange="formSync()">
            <option value="both"{" selected" if v.get('export', 'both') == 'both' else ""}>CAD + site map</option>
            <option value="cad"{" selected" if v.get('export') == 'cad' else ""}>CAD only (DXF)</option>
            <option value="map"{" selected" if v.get('export') == 'map' else ""}>Site map only (PDF)</option>
          </select></div>
        <div><label for="profile">Layout</label>
          <select id="profile" name="profile" onchange="formSync()">
            <option value="standard"{" selected" if sel == "standard" else ""}>Standard</option>
            <option value="government"{" selected" if sel == "government" else ""}>Thai government submission</option>
          </select></div>
        <div><label for="title">Map title</label>
          <input type="text" id="title" name="title" value="{val('title', 'Detailed Site Map')}"></div>
      </div>
      <div class="grid g3" style="margin-top:16px">
        <div><label for="cad_sheet">CAD sheet (paper space)</label>
          <select id="cad_sheet" name="cad_sheet" onchange="formSync()">
            {opts(["A3", "A2", "A1", "A0", "A4"], v.get('cad_sheet', 'A3'))}
            <option value="none"{" selected" if v.get('cad_sheet') == 'none' else ""}>No sheet — model space only</option>
          </select></div>
        <div><label for="cad_scale">Plot scale</label>
          <select id="cad_scale" name="cad_scale" onchange="formSync()">
            <option value="fit"{" selected" if v.get('cad_scale', 'fit') == 'fit' else ""}>Fit the extent (recommended)</option>
            {opts_scale(["500", "1000", "1250", "2000", "2500", "5000"], str(v.get('cad_scale', '')))}
          </select></div>
        <div><label for="sheet_size">Site map sheet</label>
          <select id="sheet_size" name="sheet_size">{opts(["A4","A3","A2","A1"], sheet)}</select></div>
        <div><label for="basemap">Background map</label>
          <select id="basemap" name="basemap">{basemap_opts}</select></div>
      </div>
      <div style="display:flex;gap:22px;flex-wrap:wrap;margin-top:18px">
        <label class="check"><input type="checkbox" name="codes" checked> Label unnamed buildings (B### code on the CAD sheet, type on the map)</label>
        <label class="check"><input type="checkbox" name="final"> Final (remove DRAFT watermark)</label>
      </div>
      <p class="note">Every run also gets the B&amp;W poster, one-way
      direction arrows, every mapped landmark and colour plot previews —
      nothing to remember to tick.</p>
      <p class="note" id="gov_note" style="display:none">The government
      sheet renders what its spec lists: one-way arrows and the background
      map are left off it, and apply to the CAD export and the poster
      only.</p>
      <fieldset id="gov" style="display:{'block' if sel == 'government' else 'none'}">
        <legend>Title block</legend>
        <div class="grid g2">{gov_inputs}</div>
      </fieldset>
    </div>
  </details>
</form>
<h2>Staged projects <a href="/projects">Browse and edit names →</a></h2>
<p class="note">Correct building names on a staged project and re-issue the
drawing in under a second — no re-fetch from OpenStreetMap.</p>
<h2>Already have the data <a href="/import">Import a file →</a></h2>
<p class="note">An OpenStreetMap export (<code>.osm</code> from the Export
button on openstreetmap.org) is drawn on the same layers with no network
fetch at all — useful where Overpass is blocked, or when someone sent you the
extract. Your own survey files (GeoJSON, shapefile, KML) come in the same way,
for sites OpenStreetMap has nothing mapped at.</p>
<h2>Recent generations <a href="/history">See all →</a></h2>
<div class="wide">{recent}</div>
<footer>Data © OpenStreetMap contributors (ODbL) · elevation © Copernicus</footer>
<script>
// The viewport of each sheet is computed by webui.fitting_scale() and sent
// down already worked out, so the only rule repeated here is the loop. A
// test keeps webui's arithmetic in step with sheet.py's.
var VIEWPORT = {viewport_js};
var ROUND_SCALES = {scales_js};
function viewport(size){{ return VIEWPORT[size] || null; }}
function fittingScale(w, h, size){{
  var vp = viewport(size); if(!vp) return null;
  for(var i = 0; i < ROUND_SCALES.length; i++){{
    var s = ROUND_SCALES[i];
    if(w * 1000 / s <= vp[0] && h * 1000 / s <= vp[1]) return s;
  }}
  return null;
}}
function formSync(){{
  var gov = document.getElementById('profile').value === 'government';
  document.getElementById('gov').style.display = gov ? 'block' : 'none';
  document.getElementById('gov_note').style.display = gov ? 'block' : 'none';

  var w = parseFloat(document.getElementById('width').value);
  var h = parseFloat(document.getElementById('height').value);
  // Light up the preset this extent came from, so the buttons read as the
  // current choice rather than as three things that fire and forget.
  document.querySelectorAll('.preset').forEach(function(b){{
    var on = parseFloat(b.dataset.w) === w && parseFloat(b.dataset.h) === h;
    b.classList.toggle('on', on);
  }});

  var size = document.getElementById('cad_sheet').value;
  var chosen = document.getElementById('cad_scale').value;
  var box = document.getElementById('readout');
  var big = document.getElementById('readout_scale');
  var note = document.getElementById('readout_note');
  box.className = 'readout';
  if(!(w > 0 && h > 0)){{
    big.textContent = '—';
    note.textContent = 'Enter an extent to see the plot scale.';
    return;
  }}
  if(size === 'none'){{
    big.textContent = Math.round(w) + ' × ' + Math.round(h) + ' m';
    note.textContent = 'Model space only — no sheet, so no plot scale.';
    return;
  }}
  var fit = fittingScale(w, h, size);
  if(chosen === 'fit'){{
    if(fit === null){{
      box.className = 'readout over';
      big.textContent = 'Too large for ' + size;
      note.textContent = 'Nothing down to 1:20,000 holds this extent. '
        + 'Narrow it or plot on a bigger sheet.';
      return;
    }}
    big.textContent = '1:' + fit.toLocaleString('en-US');
    var words = fit <= 1000 ? 'a site plan'
      : fit <= 2500 ? 'a detailed layout' : 'a locality map';
    note.textContent = Math.round(w) + ' × ' + Math.round(h) + ' m on '
      + size + ' plots at 1:' + fit.toLocaleString('en-US') + ' — ' + words + '.';
    if(fit >= 5000) box.className = 'readout warn';
  }} else {{
    var s = parseInt(chosen, 10);
    var vp = viewport(size);
    var fits = w * 1000 / s <= vp[0] && h * 1000 / s <= vp[1];
    big.textContent = '1:' + s.toLocaleString('en-US');
    if(fits){{
      note.textContent = 'Fits ' + size + ' at the scale you chose.';
    }} else {{
      box.className = 'readout over';
      note.textContent = 'Does not fit ' + size + ' — the sheet would crop the '
        + 'extent. ' + (fit ? 'It fits at 1:' + fit.toLocaleString('en-US') + '.'
                            : 'No round scale holds it.');
    }}
  }}
}}
document.querySelectorAll('.preset').forEach(function(b){{
  b.addEventListener('click', function(){{
    document.getElementById('width').value = b.dataset.w;
    document.getElementById('height').value = b.dataset.h;
    formSync();
  }});
}});
['width','height'].forEach(function(id){{
  document.getElementById(id).addEventListener('input', formSync);
}});
// Geolocation is a convenience, never a requirement: the button says what
// happened either way rather than failing silently, and the field stays
// typeable if the browser refuses.
(function(){{
  var btn = document.getElementById('here');
  if(!btn) return;
  if(!navigator.geolocation){{ btn.style.display = 'none'; return; }}
  btn.addEventListener('click', function(){{
    var note = document.getElementById('loc_note');
    btn.disabled = true; btn.textContent = 'Locating…';
    navigator.geolocation.getCurrentPosition(function(pos){{
      document.getElementById('coords').value =
        pos.coords.latitude.toFixed(8) + ', ' + pos.coords.longitude.toFixed(8);
      btn.disabled = false; btn.textContent = 'Use my location';
      note.textContent = 'Filled in from your device, accurate to about '
        + Math.round(pos.coords.accuracy) + ' m. Edit it if you know better.';
    }}, function(err){{
      btn.disabled = false; btn.textContent = 'Use my location';
      note.textContent = 'Could not get your location (' + err.message
        + '). Type or paste one instead.';
    }}, {{enableHighAccuracy: true, timeout: 10000}});
  }});
}})();
document.addEventListener('DOMContentLoaded', formSync);
formSync();
</script>""")

def run_page(jid: str, state: dict) -> bytes:
    """The page a generation is watched on.

    Server-rendered with the plan already in it, then Vue keeps it current
    from /run/<id>/status. That order matters: if the CDN never answers, the
    page still says what is being made and the browser's own reload still
    reaches the result — the framework makes it live, it is not what makes
    it work.
    """
    steps = "".join(
        f'<li class="{html.escape(st["state"])}">'
        f'<span class="step-mark">{"·"}</span>'
        f'<span class="step-name">{html.escape(st["name"])}</span>'
        f'<span class="step-detail">{html.escape(st["detail"])}</span></li>'
        for st in state["steps"])
    return page("Generating…", f"""
<p class="eyebrow">Run {html.escape(jid)}</p>
<h1>Generating…</h1>
<p class="lede">Each step narrates itself as it goes. Leave this tab open —
it turns into the download page by itself when the last one finishes.</p>
<div id="run" data-job="{html.escape(jid)}" class="runwrap">
  <div class="runcol">
    <ol class="steps">
      <li v-for="s in steps" :key="s.key" :class="s.state">
        <span class="step-mark" v-text="mark(s)"></span>
        <span class="step-name" v-text="s.name"></span>
        <span class="step-detail" v-text="s.detail"></span>
      </li>
    </ol>
    <div class="runbar" v-if="live"></div>
    <div class="runfoot">
      <span><b v-text="clock"></b> elapsed</span>
      <span v-text="hint"></span>
    </div>
  </div>
  <div class="runprev">
    <div class="prevhead">
      <b>Preview</b>
      <span class="prevtabs" v-if="previews.length > 1">
        <button type="button" v-for="p in previews" :key="p.kind"
                :class="{{on: p.kind === shown}}"
                @click="pick(p.kind)" v-text="p.label"></button>
      </span>
    </div>
    <div class="prevbody">
      <p class="prevwait" v-if="!previews.length">
        Nothing to show yet — the first sheet appears here the moment it is
        written, without waiting for the rest of the run.
      </p>
      <template v-for="p in previews" :key="p.kind">
        <img v-if="p.kind === shown && p.how === 'image'" :src="src(p)"
             :alt="p.label">
        <iframe v-if="p.kind === shown && p.how === 'pdf'" :src="src(p)"
                title="preview"></iframe>
      </template>
    </div>
    <p class="note prevnote" v-if="current">
      <span v-text="current.label"></span> ·
      <span v-text="size(current)"></span>
      <span v-if="live"> · refreshes as the run rewrites it</span>
    </p>
  </div>
</div>
<noscript><p class="note">This page does not refresh itself without
JavaScript. Reload it to see how far the run has got — the run itself is
unaffected.</p></noscript>
<p class="note" style="margin-top:22px">A first export in a new 1°×1° square
also downloads a ~40 MB elevation tile, which is the slowest thing here and
happens once per square.</p>
<a class="back" href="/">← Start another</a>
<footer>Data © OpenStreetMap contributors (ODbL) · elevation © Copernicus</footer>
<script>
(function(){{
  var el = document.getElementById('run');
  if(!window.Vue || !el) return;      // no CDN: the server-rendered plan stands
  var seeded = {json.dumps(state["steps"])};
  // Seeded from the server too, so reloading mid-run paints whatever the
  // run has already written instead of an empty pane until the first poll.
  var seededPreviews = {json.dumps(state.get("previews", []))};
  Vue.createApp({{
    data: function(){{
      return {{steps: seeded, live: true, started: Date.now(), clock: '0:00',
              hint: 'Working…', previews: seededPreviews,
              shown: seededPreviews.length
                     ? seededPreviews[seededPreviews.length - 1].kind : '',
              picked: false}};
    }},
    computed: {{
      current: function(){{
        var self = this;
        return this.previews.filter(function(p){{
          return p.kind === self.shown; }})[0] || null;
      }}
    }},
    methods: {{
      // ts is the file's mtime: a step that overwrites its own output
      // gets a new URL, so the browser fetches it instead of the cached one.
      src: function(p){{
        return '/run/' + el.dataset.job + '/file/' + p.kind + '?v=' + p.ts;
      }},
      size: function(p){{
        return p.bytes > 900000 ? (p.bytes / 1048576).toFixed(1) + ' MB'
                                : Math.max(1, Math.round(p.bytes / 1024)) + ' KB';
      }},
      pick: function(kind){{ this.shown = kind; this.picked = true; }},
      mark: function(s){{
        return s.state === 'done' ? '✓' : s.state === 'running' ? '▸'
             : s.state === 'failed' ? '✕' : s.state === 'skipped' ? '–' : '·';
      }},
      tick: function(){{
        var secs = Math.floor((Date.now() - this.started) / 1000);
        this.clock = Math.floor(secs / 60) + ':'
                   + (secs % 60 < 10 ? '0' : '') + (secs % 60);
      }},
      poll: function(){{
        var self = this;
        fetch('/run/' + el.dataset.job + '/status')
          .then(function(r){{ return r.json(); }})
          .then(function(d){{
            if(d.steps) self.steps = d.steps;
            if(d.previews){{
              self.previews = d.previews;
              // Follow the run to whatever it produced last, until the
              // reader picks one themselves — then leave their choice alone.
              var have = d.previews.some(function(p){{
                return p.kind === self.shown; }});
              if(!have || !self.picked){{
                if(!self.picked && d.previews.length){{
                  self.shown = d.previews[d.previews.length - 1].kind;
                }} else if(!have && d.previews.length){{
                  self.shown = d.previews[0].kind;
                }}
              }}
            }}
            if(d.state === 'done' || d.state === 'failed'){{
              self.live = false;
              self.hint = d.state === 'done' ? 'Finished — opening the files…'
                                             : 'Failed.';
              // The same URL serves the result once the run is finished, so
              // there is nowhere else to send the browser.
              location.reload();
            }} else {{
              setTimeout(self.poll, 1200);
            }}
          }})
          .catch(function(){{ setTimeout(self.poll, 3000); }});
      }}
    }},
    mounted: function(){{
      this.poll();
      this.tick();
      setInterval(this.tick, 1000);
    }}
  }}).mount('#run');
}})();
</script>""")


def result_page(rec: dict, kinds, zone: str, drive: bool,
                project_id=None) -> bytes:
    p = rec["params"]
    jid = rec["id"]

    preview = ""
    if rec.get("png"):
        b64 = base64.b64encode(Path(rec["png"]).read_bytes()).decode()
        preview = (f'<figure><img src="data:image/png;base64,{b64}" '
                   'alt="Rendered site map"></figure>')

    def file_card(kind, tag, label):
        """Preview is the big target; the small icon downloads."""
        if not rec.get(kind):
            return ""
        return (f'<span class="card"><a href="/view/{jid}/{kind}" '
                f'target="_blank" rel="noopener"><b>{tag}</b>{label}</a>'
                f'<a class="dlicon" href="/file/{jid}/{kind}" download '
                f'title="Download {label}">⤓</a></span>')

    def plain_card(kind, tag, label):
        """A file with no browser viewer and no stand-in: click to download."""
        if not rec.get(kind):
            return ""
        return (f'<span class="card"><a href="/file/{jid}/{kind}" download>'
                f'<b>{tag}</b>⤓&nbsp; {label}</a></span>')

    def download_card(kind, tag, label, preview_title):
        """The other way round, for a file with no browser viewer: clicking
        the card downloads it, and the plot preview moves to the small
        icon. Clicking 'CAD' and getting a PDF back is the wrong default."""
        if not rec.get(kind):
            return ""
        preview = ""
        if rec.get("plot"):
            preview = (f'<a class="dlicon" href="/view/{jid}/{kind}" '
                       f'target="_blank" rel="noopener" '
                       f'title="{preview_title}">◱</a>')
        return (f'<span class="card primary">'
                f'<a href="/file/{jid}/{kind}" download>'
                f'<b>{tag}</b>⤓&nbsp; {label}</a>{preview}</span>')

    dxf_tag = (f'CAD · {Path(rec["dxf"]).stat().st_size / 1024:.0f} KB'
               if rec.get("dxf") else "CAD")
    links = [
        download_card("dxf", dxf_tag, "Download DXF",
                      "Preview the A3 plot instead"),
        # Kept so the plot PDF still has a download of its own, not only the
        # preview reachable from the DXF card
        file_card("plot", "A3 plot", "Plot preview"),
        file_card("pdf", "Vector", "Site map PDF"),
        file_card("png", "300 DPI", "Site map PNG"),
        file_card("csv", "Inventory", "Buildings CSV"),
        file_card("roads", "Inventory", "Roads CSV"),
        file_card("landmarks", "Nearby", "Landmarks CSV"),
        file_card("attrs", "Attributes", "OSM tags table"),
        # Beside the combined drawing, never instead of it. Download-only:
        # the plot preview belongs to the drawing that gets issued.
        plain_card("import_dxf", "This import", "Imported file only"),
        file_card("poster", "B&W poster", "Poster PNG"),
        file_card("poster_pdf", "Vector", "Poster PDF"),
        # Download only — a browser has no GeoTIFF viewer. Keep it beside
        # the DXF: AutoCAD resolves the reference relative to the drawing,
        # so taking one without the other loses the map.
        plain_card("tif", "Backdrop", "Background map"),
    ]
    # One link for the whole package. Handing a colleague the DXF alone
    # loses the raster it references and the table saying where its lines
    # came from; this keeps them together under the names the drawing
    # expects.
    files = sum(1 for k in kinds if rec.get(k))
    if files > 1:
        links.append(f'<span class="card"><a href="/zip/{jid}" download>'
                     f'<b>All {files} files</b>⤓&nbsp; Download package'
                     f'</a></span>')

    # Only offered when the server has Google credentials — an unconfigured
    # button that always errors is worse than no button.
    if drive and rec.get("dxf"):
        links.append(f'<span class="card"><a href="/drive/{jid}">'
                     f'<b>Google Drive</b>Save this run</a></span>')

    heading = ("CAD export" if p["export"] == "cad"
               else p["title"] if p["profile"] == "standard"
               else "Project location sheet")
    return page("Export ready", f"""
<p class="eyebrow">Generated {html.escape(rec['when'])}</p>
<h1>{html.escape(heading)}</h1>
<dl class="meta">
  <div><dt>Latitude</dt><dd>{p['lat']:.8f}°</dd></div>
  <div><dt>Longitude</dt><dd>{p['lon']:.8f}°</dd></div>
  <div><dt>Coverage</dt><dd>{p['width']:.0f} × {p['height']:.0f} m</dd></div>
  <div><dt>Projection</dt><dd>UTM {zone}</dd></div>
</dl>
{preview}
<div class="files">{''.join(links)}</div>
{f'<h2>Staged as <a href="/project/{project_id}">{html.escape(rec.get("project") or "")}</a></h2><p class="note">Correct a building name there and re-issue the drawing in under a second — no re-fetch from OpenStreetMap.</p>' if project_id else ''}
<pre class="log">{html.escape(rec['log'])}</pre>
<a class="back" href="/">← Generate another</a>
<footer>Data © OpenStreetMap contributors (ODbL) · elevation © Copernicus</footer>
""")


def projects_page(rows) -> bytes:
    if rows is None:
        return page("Projects", """
<p class="eyebrow">Staging database</p><h1>Projects</h1>
<p class="lede">Nothing staged yet. Generate a CAD export and the site will
appear here, where you can correct building names and re-issue the drawing
without re-fetching from OpenStreetMap.</p>
<a class="back" href="/">← Generate</a>""")
    body = ["<table class='hist'><thead><tr><th>Project</th><th>Centre</th>"
            "<th>Extent</th><th>CRS</th><th>Buildings</th><th>Roads</th>"
            "<th></th></tr></thead><tbody>"]
    for r in rows:
        body.append(
            f"<tr><td>{html.escape(r['name'])}</td>"
            f"<td class='num'>{r['lat']:.6f}, {r['lon']:.6f}</td>"
            f"<td class='num'>{r['width_m']:.0f} × {r['height_m']:.0f} m</td>"
            f"<td class='num'>EPSG:{r['srid']}</td>"
            f"<td class='num'>{r['n_b']} ({r['n_named']} named)</td>"
            f"<td class='num'>{r['n_r']}</td>"
            f"<td class='dl'><a href='/project/{r['id']}'>Open</a></td></tr>")
    body.append("</tbody></table>")
    return page("Projects", f"""
<p class="eyebrow">{len(rows)} staged project(s)</p>
<h1>Projects</h1>
<p class="lede">Every CAD export is staged here with its label anchors already
computed. Open a project to correct building names and re-issue the drawing —
that redraw takes under a second and never touches OpenStreetMap.</p>
<div class="wide">{''.join(body)}</div>
<p class="note"><a href="/import">Import a file →</a> — an OpenStreetMap
export or your own survey data, merged into a project by name so both share
one drawing.</p>
<a class="back" href="/">← Generate</a>
<footer>Data © OpenStreetMap contributors (ODbL) · elevation © Copernicus</footer>
""")


def pager(pid, q, only, page_no, pages) -> str:
    if pages <= 1:
        return ""
    def link(n, label=None, disabled=False):
        if disabled:
            return f'<span class="pg off">{label or n}</span>'
        qs = urllib.parse.urlencode({"q": q, "only": only, "page": n})
        cls = "pg on" if n == page_no else "pg"
        return f'<a class="{cls}" href="/project/{pid}?{qs}">{label or n}</a>'

    window = [n for n in range(max(1, page_no - 2), min(pages, page_no + 2) + 1)]
    out = [link(page_no - 1, "‹ Prev", page_no == 1)]
    if window[0] > 1:
        out += [link(1), '<span class="pg off">…</span>']
    out += [link(n) for n in window]
    if window[-1] < pages:
        out += ['<span class="pg off">…</span>', link(pages)]
    out.append(link(page_no + 1, "Next ›", page_no == pages))
    return f'<div class="pager">{"".join(out)}</div>'


def project_page(d, note: str, q: str, page_no: int, only: str,
                 per_page: int) -> bytes:
    pid = d["pid"]
    proj, buildings, roads, sources = (
        d["proj"], d["buildings"], d["roads"], d["sources"])
    total_all, unnamed_all = d["total_all"], d["unnamed_all"]
    matched, pages = d["matched"], d["pages"]
    rows = []
    for b in buildings:
        placeholder = b["code"] or ""
        current = b["display_name"] or ""
        is_code = current == placeholder
        rows.append(
            f"<tr><td class='num'>{html.escape(b['feature_id'])}</td>"
            f"<td class='num'>{html.escape(placeholder)}</td>"
            f"<td class='num'>{(b['area_m2'] or 0):,.0f} m²</td>"
            f"<td>{html.escape(b['source'] or '')}</td>"
            f"<td><input type='text' name='name::{html.escape(b['feature_id'])}'"
            f" value='{'' if is_code else html.escape(current)}'"
            f" placeholder='{html.escape(placeholder)}'></td></tr>")

    road_rows = "".join(
        f"<tr><td>{html.escape(r['road_name'] or '—')}</td>"
        f"<td class='num'>{html.escape(r['road_ref'] or '—')}</td>"
        f"<td>{html.escape(r['highway_type'] or '')}</td>"
        f"<td class='num'>{r['carriageway_m']:.1f} m</td>"
        f"<td class='num'>{r['length_m']:,.0f} m</td>"
        f"<td class='num'>{r['segments']}</td></tr>" for r in roads)

    grouped: dict[str, list] = {}
    for row in sources:
        grouped.setdefault(row["source"], []).append(row)
    source_rows = "".join(
        f"<tr><td>{html.escape(src)}</td>"
        f"<td class='num'>{sum(r['count'] for r in rs):,}</td>"
        f"<td>{html.escape(', '.join(f'{r["count"]} {r["feature_class"]}' for r in rs))}</td></tr>"
        for src, rs in sorted(grouped.items(),
                              key=lambda kv: -sum(r["count"] for r in kv[1])))

    runs_rows = history_table(d.get("runs") or [])
    if not (d.get("runs") or []):
        runs_rows = ('<p class="note">No run on this machine wrote into this '
                     "project — it was staged by an import or by the command "
                     "line.</p>")

    banner = f'<div class="ok">{html.escape(note)}</div>' if note else ""
    return page(f"{proj['name']}", f"""
<p class="eyebrow">Project {pid} · EPSG:{proj['srid']}</p>
<h1>{html.escape(proj['name'])}</h1>
{banner}
<dl class="meta">
  <div><dt>Centre</dt><dd>{proj['lat']:.6f}, {proj['lon']:.6f}</dd></div>
  <div><dt>Extent</dt><dd>{proj['width_m']:.0f} × {proj['height_m']:.0f} m</dd></div>
  <div><dt>Buildings</dt><dd>{total_all}</dd></div>
  <div><dt>Named</dt><dd>{total_all - unnamed_all} verified</dd></div>
</dl>

<h2>Sources</h2>
<div class="wide"><table class="hist"><thead><tr><th>Source</th>
<th>Features</th><th>What</th></tr></thead>
<tbody>{source_rows or '<tr><td>Nothing staged</td></tr>'}</tbody></table></div>

<h2>Roads</h2>
<div class="wide"><table class="hist"><thead><tr><th>Name</th><th>Route no.</th>
<th>Class</th><th>Carriageway</th><th>Total length</th><th>Ways</th></tr></thead>
<tbody>{road_rows or '<tr><td>No named roads</td></tr>'}</tbody></table></div>

<h2>Buildings <span style="font-weight:400;font-size:13px;color:var(--soft)">
{total_all} total · {unnamed_all} still need a verified name</span></h2>

<form method="get" action="/project/{pid}" class="filters">
  <input type="text" name="q" value="{html.escape(q)}"
         placeholder="Search code, name or feature id…">
  <select name="only">
    <option value="all"{" selected" if only == "all" else ""}>All buildings</option>
    <option value="unnamed"{" selected" if only == "unnamed" else ""}>Needs a name</option>
    <option value="named"{" selected" if only == "named" else ""}>Already named</option>
  </select>
  <button type="submit">Filter</button>
  {'<a class="clear" href="/project/' + str(pid) + '">Clear</a>'
   if (q or only != "all") else ''}
</form>
<p class="note">Showing {len(buildings)} of {matched} matching
({(page_no - 1) * per_page + 1 if buildings else 0}–{(page_no - 1) * per_page + len(buildings)}).
Type a verified name to replace its code — blank keeps the code. Saving
applies to this page only.</p>

<form method="post" action="/project/{pid}/save?{urllib.parse.urlencode({'q': q, 'only': only, 'page': page_no})}">
  <div class="wide">
  <table class="hist"><thead><tr><th>Feature</th><th>Code</th><th>Area</th>
  <th>Source</th><th>Verified name</th></tr></thead>
  <tbody>{''.join(rows) or '<tr><td colspan="5">No buildings match.</td></tr>'}</tbody></table></div>
  <button type="submit">Save names on this page</button>
</form>
{pager(pid, q, only, page_no, pages)}

<h2>Runs that made this <a href="/history">All runs →</a></h2>
{runs_rows}

<h2>Re-issue drawing</h2>
<form method="post" action="/project/{pid}/redraw">
  <div class="grid g3">
    <div><label for="cad_sheet">Sheet</label>
      <select id="cad_sheet" name="cad_sheet">
        <option value="A3">A3</option><option value="A2">A2</option>
        <option value="A1">A1</option><option value="A0">A0</option>
        <option value="A4">A4</option>
        <option value="none">No sheet — model space only</option>
      </select></div>
    <div><label for="cad_scale">Plot scale</label>
      <select id="cad_scale" name="cad_scale">
        <option value="fit">Fit the extent (recommended)</option>
        <option value="500">1:500</option><option value="1000">1:1,000</option>
        <option value="1250">1:1,250</option>
        <option value="2000">1:2,000</option>
        <option value="2500">1:2,500</option>
        <option value="5000">1:5,000</option>
      </select></div>
    <div style="display:flex;align-items:flex-end">
      <button type="submit" style="margin-top:0" data-idle="Re-issue drawing"
        data-busy="Re-issuing…">Re-issue drawing</button>
    </div>
  </div>
  <label class="check"><input type="checkbox" name="codes" checked>
    Label unnamed buildings with their B### code</label>
  <p class="note">The codes are the handle this page edits against and the
  key of <code>building_inventory.csv</code>. Untick for a sheet that names
  only what the source named — where nothing is named, that is a building
  layer with no labels at all.</p>
  <div id="busy"><div class="load" data-estimate="15">
    <div class="load-head"><span class="load-title">Re-issuing…</span>
      <span class="load-time">0:15</span></div>
    <div class="load-bar"></div>
    <p class="load-note">Redrawing from the staging database — plain SELECTs,
    no Overpass and no elevation tile, so this is the fast path.</p>
    <p class="load-note load-over" style="display:none">Over the estimate —
    still working.</p>
  </div></div>
</form>
<a class="back" href="/projects">← Projects</a>
<footer>Data © OpenStreetMap contributors (ODbL) · elevation © Copernicus</footer>
""")


def import_page(projects, osm_types, osm_type_labels, basemap_choices,
                note: str, error: str) -> bytes:
    options = "".join(
        f'<option value="{html.escape(p["name"])}">{html.escape(p["name"])}'
        f" (project {p['id']})</option>" for p in projects)
    banner = (f'<div class="err">{html.escape(error)}</div>' if error else
              f'<div class="ok">{html.escape(note)}</div>' if note else "")
    types = "".join(
        f'<label class="check"><input type="checkbox" name="t_{t}" '
        f'value="1" checked> {html.escape(osm_type_labels[t])}</label>'
        for t in osm_types)
    basemap_opts = "".join(f'<option value="{k}">{html.escape(label)}</option>'
                           for k, label in basemap_choices.items())
    return page("Import a file", f"""
<p class="eyebrow">An OSM export or your own survey → CAD</p>
<h1>Import a file</h1>
<p class="lede">Two kinds of file are drawn here, and the right converter is
picked from the extension. An <b>OpenStreetMap export</b> (.osm) becomes the
same NCS-layered drawing the generator makes, with no network fetch at all —
useful where Overpass is blocked or the area was exported for you. Your own
<b>GIS data</b> — plots, access roads, equipment pads — is drawn in true UTM
metres on the same layers. Either is merged into a project, so a survey and
an extract can share one drawing.</p>
{banner}
<form method="post" action="/import" enctype="multipart/form-data"
     >
  <div class="grid g2">
    <div><label for="files">File(s)</label>
      <input type="file" id="files" name="files" multiple
             accept=".osm,.xml,.gz,.bz2,.geojson,.json,.gpkg,.kml,.gml,.zip">
      </div>
    <div><label for="project">Merge into project</label>
      <input type="text" id="project" name="project" list="projects"
             placeholder="new or existing project name">
      <datalist id="projects">{options}</datalist></div>
  </div>
  <div class="grid g3" style="margin-top:16px">
    <div><label for="epsg">Coordinate system (optional)</label>
      <input type="text" id="epsg" name="epsg"
             placeholder="auto: UTM zone from the data"></div>
    <div><label for="name_field">Name attribute (GIS only)</label>
      <input type="text" id="name_field" name="name_field"
             placeholder="auto: name, PLOT_NAME, label…"></div>
    <div><label for="layer">CAD layer (GIS only)</label>
      <input type="text" id="layer" name="layer"
             placeholder="e.g. C-PROP-LINE"></div>
    <div><label for="width">Line width, m (GIS only)</label>
      <input type="number" id="width" name="width" step="any" min="0"
             value="6"></div>
  </div>
  <fieldset style="margin-top:16px">
    <legend>OpenStreetMap files</legend>
    <div class="checks">{types}</div>
    <div class="grid g3" style="margin-top:16px">
      <div><label for="bbox">Crop to box (optional)</label>
        <input type="text" id="bbox" name="bbox"
               placeholder="S,W,N,E in degrees"></div>
      <div><label for="layer_by">Split layers by tag</label>
        <input type="text" id="layer_by" name="layer_by"
               placeholder="e.g. highway, building"></div>
      <div><label for="basemap">Background map</label>
        <select id="basemap" name="basemap">{basemap_opts}</select></div>
    </div>
    <div class="checks" style="margin-top:16px">
      <label class="check"><input type="checkbox" name="attributes"
        value="1" checked> Attach OSM tags as XDATA</label>
      <label class="check"><input type="checkbox" name="all_poi" value="1">
        Every amenity, not only civic landmarks</label>
      <label class="check"><input type="checkbox" name="names_only" value="1">
        Named buildings only (no B### codes)</label>
      <label class="check"><input type="checkbox" name="mono" value="1">
        Monochrome (แผนที่สังเขป)</label>
      <label class="check"><input type="checkbox" name="replace" value="1">
        Replace the project instead of merging into it</label>
    </div>
    <p class="note" style="margin-top:10px">Which feature types to import,
    and how they are drawn. XDATA is what puts the source tags on each
    entity — select a building in AutoCAD and LIST shows them. Merging is
    the default, so importing one feature type at a time from the same file
    builds up a single project. These apply to OpenStreetMap files;
    monochrome applies to GIS files too.</p>
  </fieldset>
  <button type="submit" data-idle="Import and draw"
    data-busy="Importing…">Import and draw</button>
  <div id="busy"><div class="load" data-estimate="20">
    <div class="load-head"><span class="load-title">Importing…</span>
      <span class="load-time">0:20</span></div>
    <div class="load-bar"></div>
    <p class="load-note">Reading the file, reprojecting to UTM and drawing.
    No network fetch, so this is usually quick.</p>
    <p class="load-note load-over" style="display:none">Over the estimate —
    still working. A large export takes longer.</p>
  </div></div>
</form>
<p class="note">OpenStreetMap: .osm or .xml from the Export button on
openstreetmap.org, optionally .gz or .bz2. GIS: GeoJSON, GeoPackage, KML,
GML, or a .zip holding a shapefile set — files without a declared CRS are
assumed to be latitude/longitude. Upload the two kinds separately; a .osm.pbf
has to be converted first (<code>osmium cat -o map.osm map.osm.pbf</code>).</p>
<a class="back" href="/">← Generate from OpenStreetMap</a>
<footer>Data © OpenStreetMap contributors (ODbL) · elevation © Copernicus</footer>
<script>
</script>""")
