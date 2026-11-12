"""The simple report: one folder with an HTML page, the plots and the table.

This module has no Qt. The «Polars» window calls `build`; a script or the
run queue can call it too:

    .venv\\Scripts\\python.exe fs_report.py cases\\plane_aoa0 cases\\plane_aoa3 ^
        cases\\plane_aoa6 --out reports\\plane --lang en

The folder:

    index.html          settings, reference values, table, plots, images
    polar.csv           the table (fs_polars.write_csv)
    polar_<kind>.png/svg  each plot, and polar_all.* (all plots)
    cp_body.png, slice_dU.png  pictures of one case (if it has fields)

The page uses only relative links: you can move or zip the folder.
The language of the page is the interface language (`--lang` here).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if __name__ == "__main__":                     # a script: the program folder
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import fs_compat  # noqa: F401  (first of our modules)

import fs_case as fc
import fs_i18n
import fs_library as lib
import fs_physics as phys
import fs_polars as pol
from fs_i18n import tr

IMAGE_W = 1100


@dataclass
class Report:
    folder: str
    index: str = ""
    images: list = field(default_factory=list)   # (file, caption)
    notes: list = field(default_factory=list)    # what is not in the report


def _e(text) -> str:
    return html.escape(str(text))


# ==========================================================================
# Case settings
# ==========================================================================

def _cfg(p: pol.Point):
    info = p.info
    return lib.cfg_from_dict(info.cfg) if info is not None and info.has_cfg else None


def settings_rows(points: list[pol.Point]) -> list[tuple[str, list[str]]]:
    """(label, value per case). An empty value: the case has no studio.json."""
    prepared = [(p, _cfg(p)) for p in points]

    def row(label, fn):
        out = []
        for p, cfg in prepared:
            try:
                out.append("" if cfg is None else str(fn(p, cfg)))
            except (AttributeError, KeyError, TypeError, ValueError):
                out.append("")
        return (label, out)

    def size(p, c):
        d = [c.bbox_max[i] - c.bbox_min[i] for i in range(3)]
        return tr("{x} × {y} × {z}", x=phys.fmt_len(d[0]),
                  y=phys.fmt_len(d[1]), z=phys.fmt_len(d[2]))

    def layers(p, c):
        if not c.mesh.add_layers:
            return tr("none")
        text = tr("{n} × {g:g}", n=c.mesh.n_layers, g=c.mesh.expansion)
        cover = p.info.layers if p.info is not None else -1.0
        if cover >= 0:
            text += tr(", on {v:.0f}% of the faces", v=cover * 100)
        return text

    def run_length(p, c):
        s = c.solver
        if s.steady:
            done = p.info.iterations if p.info is not None else 0
            return tr("{done} of {n} iterations", done=done or "—",
                      n=s.n_iterations)
        text = tr("{t:g} s, Δt {dt:g} s, Co max {co:g}", t=s.end_time,
                  dt=s.time_step, co=s.max_courant)
        if c.warm_start:
            text += tr("; warm start {n} iterations", n=s.warm_iterations)
        return text

    def kind(p, c):
        return tr("steady") if c.solver.steady else tr("transient")

    return [
        (tr("geometry file"), [os.path.basename(p.info.stl_source)
                               if p.info is not None and p.info.stl_source
                               else "—" for p, _ in prepared]),
        row(tr("body size (X × Y × Z)"), size),
        (tr("cells"), [phys.fmt_cells(p.info.cells)
                       if p.info is not None and p.info.cells else "—"
                       for p, _ in prepared]),
        row(tr("prism layers"), layers),
        row(tr("solver"), lambda p, c: fc.solver_name(c)),
        row(tr("kind of run"), kind),
        row(tr("length of run"), run_length),
        row(tr("turbulence model"), lambda p, c: c.flow.turbulence),
        row(tr("U∞, m/s"), lambda p, c: "%.4g" % c.flow.u_inf),
        row(tr("ρ, kg/m³"), lambda p, c: "%.4g" % c.flow.rho),
        row(tr("ν, m²/s"), lambda p, c: "%.4g" % c.flow.nu),
        row("Re (lRef)", lambda p, c: "%.3g" % phys.reynolds(
            c.flow.u_inf, c.flow.l_ref, c.flow.nu)),
        row(tr("Mach"), lambda p, c: "%.3f" % c.flow.mach),
        row(tr("angle of attack α"), lambda p, c: "%g°" % c.flow.aoa_deg),
        row(tr("sideslip angle β"), lambda p, c: "%g°" % c.flow.aos_deg),
        row(tr("far field"), lambda p, c: c.flow.farfield_mode),
        (tr("run"), [p.info.run_status or "—" if p.info is not None else "—"
                     for p, _ in prepared]),
        (tr("run time"), [lib.fmt_seconds(p.info.seconds)
                          if p.info is not None else "—"
                          for p, _ in prepared]),
    ]


def split_rows(rows):
    """(common, different): rows with one value for all cases, and the rest."""
    common, different = [], []
    for label, values in rows:
        vals = [v for v in values if v != ""]
        if vals and len(set(vals)) == 1 and len(vals) == len(values):
            common.append((label, vals[0]))
        else:
            different.append((label, values))
    return common, different


# ==========================================================================
# Pictures of one case
# ==========================================================================

def image_case(points: list[pol.Point]) -> pol.Point | None:
    """The case for the pictures: a converged one with fields, in the middle
    of the angle range. None if no case has fields."""
    have = [p for p in points if p.info is not None and p.info.times]
    if not have:
        return None
    good = [p for p in have if p.converged] or have
    good = pol.sort_points(good)
    return good[len(good) // 2]


def _cp_limits(body) -> tuple[float, float]:
    import numpy as np
    v = np.asarray(body.cell_data["Cp"], dtype=float)
    v = v[np.isfinite(v)]
    h = float(np.percentile(np.abs(v), 98.0)) if v.size else 1.0
    h = max(h, 1e-3)
    import fs_views as fv
    lo, hi, _ = fv.nice_limits(-h, h, True)
    return lo, hi


def render_images(loaded, folder: Path, progress=None,
                  steady: bool = True) -> list[tuple[str, str]]:
    """Cp on the body (top, 3D) and a slice of the speed through the body.

    `loaded` is a `fs_viewcase.LoadedCase`. Call this in the thread that
    may use OpenGL (the window thread in the program).
    """
    import fs_post as post
    import fs_views as fv

    say = progress or (lambda text: None)
    out = []
    view, name = loaded.view, loaded.name
    body = fv._merged(view.bodies).copy()
    post.add_cp(body, view.fs)
    if "Cp" in body.cell_data:
        say(tr("Cp on the body…"))
        lim = _cp_limits(body)
        frames = [[fv.render_body_frame(body, view.fs, view=v, field="Cp",
                                        limits=lim, size=(900, 600), label=lbl)
                   for v, lbl in (("top", tr("top")), ("iso", "3D"))]]
        when = (tr("iteration {t:g}", t=loaded.time) if steady
                else tr("t = {t:g} s", t=loaded.time))
        head = tr("{name} — pressure coefficient Cp on the body, "
                  "U∞ = {u:g} m/s, {when}", name=name, u=view.fs.u_inf,
                  when=when)
        path = folder / "cp_body.png"
        fv.compose(frames, title=head, bar=(fv.spec_by_key("cp"), lim[0], lim[1]),
                   path=str(path))
        out.append((path.name, tr("Cp on the body: top view and 3D view")))
    say(tr("Slice through the body…"))
    b = fv.view_bounds(view.bodies, view.volume, pad=0.0)
    pos = 0.5 * (b[2] + b[3])
    path = folder / "slice_dU.png"
    fv.slice_report([view], field="dU", axis=1, pos=pos, name=name,
                    size=(1400, 560), path=str(path))
    out.append((path.name, tr("Slice y = {v:.0f} mm through the middle of the "
                              "body: speed change along the flow",
                              v=pos * 1000)))
    return out


# ==========================================================================
# The page
# ==========================================================================

CSS = """
body{font:14px/1.45 system-ui,'Segoe UI',sans-serif;color:#0b0b0b;
background:#fcfcfb;margin:0;padding:24px 32px;max-width:1280px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px;
border-bottom:1px solid #e4e3df;padding-bottom:4px}
.sub{color:#52514e}table{border-collapse:collapse;margin:6px 0}
td,th{border:1px solid #e4e3df;padding:3px 8px;text-align:left;
vertical-align:top}th{background:#f1f0ec;font-weight:600}
td.n{text-align:right;font-variant-numeric:tabular-nums}
.bad{color:#c0392b}.warn{color:#9a6200}.ok{color:#1d7a46}
.plots{display:flex;flex-wrap:wrap;gap:12px}.plots figure{margin:0}
img{max-width:100%;height:auto;border:1px solid #e4e3df}
figcaption{color:#52514e;font-size:13px}
ul{margin:4px 0 4px 20px;padding:0}
"""


def _table(head: list, rows: list, numeric: tuple = ()) -> str:
    out = ["<table><tr>" + "".join(f"<th>{_e(h)}</th>" for h in head)
           + "</tr>"]
    for r in rows:
        cells = []
        for i, v in enumerate(r):
            cls = " class='n'" if i in numeric else ""
            cells.append(f"<td{cls}>{v}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def coeff_table_html(points: list[pol.Point]) -> str:
    cs = pol.has_cs(points)
    head = [tr("case"), "α, °", "Cl", "Cd", "Cm", "L/D"]
    if cs:
        head.append("Cs")
    head += [tr("state"), tr("averaged over")]
    rows = []
    for p in points:
        def cell(name):
            v, s = p.value(name), p.std(name)
            if v is None:
                return "—"
            return f"{pol.fmt(v)} <span class='sub'>± {pol.fmt(s, 2)}</span>"
        ld, lds = p.ld, p.ld_std
        r = [_e(p.name), _e(pol.fmt(p.alpha)), cell("Cl"), cell("Cd"),
             cell("CmPitch"),
             "—" if ld is None else f"{pol.fmt(ld)} <span class='sub'>± "
             f"{pol.fmt(lds, 2)}</span>"]
        if cs:
            r.append(cell("Cs"))
        cls = "ok" if p.converged else "warn"
        state = f"<span class='{cls}'>{_e(pol.verdict_word(p.verdict))}</span>"
        why = p.why()
        if why:
            state += "<br><span class='sub'>" + _e(why).replace("\n", "<br>") \
                + "</span>"
        r.append(state)
        win = _e(p.window.text())
        if p.steady and math.isfinite(p.residual_max):
            win += "<br><span class='sub'>" + _e(tr(
                "max initial residual {v:.1e}", v=p.residual_max)) + "</span>"
        r.append(win)
        rows.append(r)
    return _table(head, rows, numeric=tuple(range(1, len(head) - 2)))


def ref_html(points: list[pol.Point]) -> str:
    ref = pol.common_ref(points)
    cofr = ", ".join("%g" % x for x in ref.cofr)
    items = [
        tr("Aref = {v:.6g} m²", v=ref.a_ref),
        tr("lRef = {v:.6g} m", v=ref.l_ref),
        tr("U∞ = {v:.6g} m/s", v=ref.u_inf),
        tr("ρ = {v:.5g} kg/m³", v=ref.rho),
        tr("moment center CofR = ({v}) m", v=cofr),
    ]
    text = "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>"
    text += "<p class='sub'>" + _e(tr(
        "Cl, Cd, Cs = F / (½ ρ U∞² Aref); Cm = M / (½ ρ U∞² Aref lRef), "
        "about CofR. The values come from the header of the OpenFOAM "
        "coefficient file of the first case ({src}).", src=ref.source or "—")) \
        + "</p>"
    return text


def _favicon() -> str:
    """The «Polars» logo as the tab icon of the page (in the page itself:
    the folder stays one folder)."""
    import base64
    path = Path(__file__).resolve().with_name("icons") / "polars-32.png"
    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return ""
    return f"<link rel='icon' type='image/png' href='data:image/png;base64,{data}'>"


def page(points, title: str, plots: list, images: list, notes: list,
         csv_name: str) -> str:
    when = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lang = fs_i18n.language()
    parts = [f"<!doctype html><html lang='{lang}'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>{_e(title)}</title>{_favicon()}<style>{CSS}</style>"
             f"</head><body>"]
    parts.append(f"<h1>{_e(title)}</h1>")
    parts.append("<p class='sub'>" + _e(tr(
        "OpenFOAM Studio, {when}. {n} cases.", when=when, n=len(points)))
        + "</p>")

    found = pol.checks(points)
    if found:
        parts.append("<ul>" + "".join(
            f"<li class='{c.level}'>{_e(c.text)}</li>" for c in found) + "</ul>")

    parts.append(f"<h2>{_e(tr('Reference values'))}</h2>")
    parts.append(ref_html(points))

    parts.append(f"<h2>{_e(tr('Coefficients'))}</h2>")
    parts.append(coeff_table_html(points))
    parts.append("<p class='sub'>" + _e(tr(
        "± is the standard deviation in the averaging window. Steady: the "
        "last iterations; transient: a time window. The table as CSV:"))
        + f" <a href='{_e(csv_name)}'>{_e(csv_name)}</a></p>")

    parts.append(f"<h2>{_e(tr('Polars'))}</h2><div class='plots'>")
    for png, svg, caption in plots:
        parts.append(f"<figure><img src='{_e(png)}' width='560' alt='{_e(caption)}'>"
                     f"<figcaption>{_e(caption)} · <a href='{_e(svg)}'>SVG</a>"
                     f"</figcaption></figure>")
    parts.append("</div><p class='sub'>" + _e(tr(
        "Open markers: the point is not converged. Bars: ± standard "
        "deviation.")) + "</p>")

    common, different = split_rows(settings_rows(points))
    parts.append(f"<h2>{_e(tr('Case settings'))}</h2>")
    if common:
        parts.append("<p>" + _e(tr("The same for all cases:")) + "</p>")
        parts.append(_table([tr("parameter"), tr("value")],
                            [[_e(a), _e(b)] for a, b in common]))
    if different and len(points) > 1:
        parts.append("<p>" + _e(tr("Different between the cases:")) + "</p>")
        parts.append(_table([tr("parameter")] + [p.name for p in points],
                            [[_e(a)] + [_e(v or "—") for v in vals]
                             for a, vals in different]))
    elif different:
        parts.append(_table([tr("parameter"), tr("value")],
                            [[_e(a), _e(vals[0] or "—")] for a, vals in different]))

    if images:
        parts.append(f"<h2>{_e(tr('Results pictures'))}</h2>")
        for name, caption in images:
            parts.append(f"<figure><img src='{_e(name)}' width='{IMAGE_W}' "
                         f"alt='{_e(caption)}'><figcaption>{_e(caption)}"
                         f"</figcaption></figure>")
    if notes:
        parts.append("<ul class='sub'>" + "".join(
            f"<li>{_e(n)}</li>" for n in notes) + "</ul>")
    parts.append("</body></html>")
    return "\n".join(parts)


def default_title(points: list[pol.Point]) -> str:
    groups = pol.groups_of(points)
    if len(groups) == 1:
        key = pol.series_key(points[0].name) if points else None
        if key:
            return tr("Polar: {name}", name=key[0])
    return tr("Polar")


def build(points: list[pol.Point], folder: str | os.PathLike, *,
          title: str = "", images: bool = True, image_point=None,
          loader=None, progress=None) -> Report:
    """Write the report folder. Returns what was written.

    `image_point`: the case for the pictures (default `image_case`).
    `loader(case_dir)`: reads the case for the pictures (default
    `fs_viewcase.load_case`; the window reads it in a worker thread).
    """
    say = progress or (lambda text: None)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    rep = Report(str(folder))
    title = title or default_title(points)

    say(tr("Table…"))
    csv_name = "polar.csv"
    pol.write_csv(folder / csv_name, points)

    say(tr("Plots…"))
    pol.save_plots(points, folder, "polar")
    plots = [(f"polar_{k}.png", f"polar_{k}.svg", tr(pol.TITLES[k]))
             for k in pol.KINDS]

    if images:
        target = image_point or image_case(points)
        if target is None:
            rep.notes.append(tr("No case has result fields on this computer: "
                                "the report has no pictures."))
        else:
            say(tr("Reading the case “{name}”…", name=target.name))
            try:
                if loader is None:
                    import fs_viewcase as vc
                    loaded = vc.load_case(target.path)
                else:
                    loaded = loader(target.path)
                rep.images = render_images(loaded, folder, say, target.steady)
            except (ValueError, KeyError, RuntimeError, MemoryError) as exc:
                rep.notes.append(tr("Pictures of “{name}” could not be made: "
                                    "{why}", name=target.name, why=exc))
    say(tr("Page…"))
    text = page(points, title, plots, rep.images, rep.notes, csv_name)
    rep.index = str(folder / "index.html")
    Path(rep.index).write_text(text, encoding="utf-8")
    return rep


# ==========================================================================
# Command line
# ==========================================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Polars and a simple report "
                                 "from saved cases (no window).")
    ap.add_argument("cases", nargs="+", help="case folders")
    ap.add_argument("--out", required=True, help="report folder")
    ap.add_argument("--lang", choices=sorted(fs_i18n.LANGUAGES), default=None)
    ap.add_argument("--title", default="")
    ap.add_argument("--steady-n", type=int, default=pol.STEADY_N,
                    help="steady: average over the last N iterations")
    ap.add_argument("--frac", type=float, default=pol.TRANSIENT_FRAC,
                    help="transient: average over this part of the time")
    ap.add_argument("--t-from", type=float, default=None,
                    help="transient: average from this time, s")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--csv-only", action="store_true",
                    help="write only polar.csv into --out")
    a = ap.parse_args(argv)
    if a.lang:
        fs_i18n.set_language(a.lang)
    points = pol.read_points(a.cases, steady_n=a.steady_n, frac=a.frac,
                             t_from=a.t_from)
    if a.csv_only:
        Path(a.out).mkdir(parents=True, exist_ok=True)
        print(pol.write_csv(Path(a.out) / "polar.csv", points))
        return 0
    rep = build(points, a.out, title=a.title, images=not a.no_images,
                progress=lambda t: print(t, flush=True))
    for n in rep.notes:
        print("!", n)
    print(rep.index)
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    sys.exit(main())
