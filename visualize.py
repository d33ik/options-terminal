"""
DAX Options Terminal — дашборд опціонної аналітики.

Запуск:  python visualize.py   (або подвійний клік на запустити.bat)
"""

import sys, logging, webbrowser, json
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).parent / "src"))

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from eurex_provider import EurexProvider
from futures_price  import fetch_dax_spot
from analytics      import calc_max_pain, calc_put_call_ratio, estimate_underlying_parity
from gex            import calc_gex
from database       import (init_db, save_chains, save_analytics, save_gex,
                            load_latest, load_previous, prune_sessions)
from models         import OptionStrikeRaw

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

# ─────────────────────────────────────────────────────────────────────────────
# Режим сервера. Локально нічого не змінюється — усе вмикається змінними
# оточення, які виставляє тільки GitHub Actions.
#   OPTIONS_OUT_DIR  куди класти HTML (за замовчуванням — папка скрипта)
#   OPTIONS_HEADLESS не відкривати браузер + Plotly з CDN замість вшитого
# ─────────────────────────────────────────────────────────────────────────────
import os

HEADLESS = bool(os.environ.get("OPTIONS_HEADLESS"))
OUT_DIR  = Path(os.environ.get("OPTIONS_OUT_DIR") or Path(__file__).parent)
# Вшитий Plotly (~5 МБ) потрібен лише щоб файл відкривався офлайн.
# На хостингу сторінка й так вантажиться з мережі — беремо CDN і 0.6 МБ.
PLOTLY_JS = "cdn" if HEADLESS else True



# ── Палітра ──────────────────────────────────────────────────────────────────
VOID, SURFACE, RULE = "#000000", "#0A0A0B", "#1A1A1D"
CHALK, ASH          = "#FFFFFF", "#6E6E76"     # puts / calls
SPOT_C, PAIN_C      = "#22D3EE", "#E8A317"     # spot / max pain
GEX_POS, GEX_NEG    = "#22D3EE", "#FB7185"
D_OPEN, D_CLOSE     = "#2DD4A7", "#FB7185"
TEXT, MUTED, FAINT  = "#E8E8EA", "#6E6E76", "#45454C"

ZOOM       = 0.18   # видима зона по X: ±18 % від spot
VOL_CAP    = 700    # фіксована шкала Volume
SEPARATORS = ". "   # [десятковий, тисячний] → «25 200», «37.40M»

MONO = "'IBM Plex Mono', ui-monospace, 'SF Mono', Consolas, monospace"


def _fmt(v, f=","):
    return f"{v:{f}}" if v is not None else "N/A"

def _sp(v, f=",.0f"):
    """Число з пробілом як роздільником тисяч."""
    return _fmt(v, f).replace(",", "\u2009") if v is not None else "—"

def _xrange(c, p=ZOOM):
    return [c * (1 - p), c * (1 + p)] if c else None

def _filter(rows, center, p=ZOOM + 0.05):
    if not center:
        return rows
    lo, hi = center * (1 - p), center * (1 + p)
    keep = [r for r in rows if lo <= r["strike"] <= hi]
    return keep if len(keep) >= 5 else rows

def _bar_width(xs):
    if len(xs) < 2:
        return 50
    return sorted({round(xs[i + 1] - xs[i]) for i in range(len(xs) - 1)})[0] * 0.82


# ── Головна фігура ───────────────────────────────────────────────────────────
def build_figure(data, prev_lookup):
    """
    7 трейсів на expiry:
      0 call_oi  1 put_oi  2 call_vol  3 put_vol  4 gex  5 d_call  6 d_put
    Легенди Plotly немає — ключ кольорів у HTML, тож зникнути не може.
    """
    data = sorted([d for d in data if d["strikes"]], key=lambda d: d["expiry"])

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.62, 0.38], vertical_spacing=0.025)

    for i, d in enumerate(data):
        a      = d["analytics"]
        center = a.get("futures_px") or a.get("underlying")
        rows   = _filter(d["strikes"], center)
        xs     = [r["strike"] for r in rows]
        bw     = _bar_width(xs)
        base   = dict(marker_line_width=0, visible=(i == 0), showlegend=False)

        fig.add_trace(go.Bar(
            x=xs, y=[r["call_oi"] for r in rows], width=bw,
            name="Call OI", marker_color=ASH,
            hovertemplate="<b>%{x}</b>   Call OI  <b>%{y}</b><extra></extra>", **base,
        ), row=1, col=1)

        fig.add_trace(go.Bar(
            x=xs, y=[-r["put_oi"] for r in rows], width=bw,
            name="Put OI", marker_color=CHALK,
            customdata=[r["put_oi"] for r in rows],
            hovertemplate="<b>%{x}</b>   Put OI  <b>%{customdata}</b><extra></extra>", **base,
        ), row=1, col=1)

        fig.add_trace(go.Bar(
            x=xs, y=[r["call_vol"] or 0 for r in rows], width=bw,
            name="Call Vol", marker_color=ASH, opacity=0.8,
            hovertemplate="<b>%{x}</b>   Call Vol  <b>%{y}</b><extra></extra>", **base,
        ), row=2, col=1)

        fig.add_trace(go.Bar(
            x=xs, y=[-(r["put_vol"] or 0) for r in rows], width=bw,
            name="Put Vol", marker_color=CHALK, opacity=0.8,
            customdata=[r["put_vol"] or 0 for r in rows],
            hovertemplate="<b>%{x}</b>   Put Vol  <b>%{customdata}</b><extra></extra>", **base,
        ), row=2, col=1)

        # GEX
        grows = _filter(d["gex_data"], center) if d["gex_data"] else []
        gx = [g["strike"] for g in grows]
        gy = [g["gex"] / 1e6 for g in grows]
        giv = [f"IV  {g['call_iv']:.1f}% C / {g['put_iv']:.1f}% P"
               if g["call_iv"] and g["put_iv"] else "" for g in grows]
        fig.add_trace(go.Bar(
            x=gx, y=gy, width=(_bar_width(gx) if gx else 50),
            name="GEX", visible=False, showlegend=False, marker_line_width=0,
            marker_color=[GEX_POS if v >= 0 else GEX_NEG for v in gy],
            customdata=giv,
            hovertemplate="<b>%{x}</b>   GEX  <b>%{y:.2f}M €</b><br>%{customdata}<extra></extra>",
        ), row=1, col=1)

        # Δ OI
        prev = prev_lookup.get(d["expiry"], {})
        dxs, dcall, dput = [], [], []
        for r in rows:
            p = prev.get(r["strike"])
            if p:
                dxs.append(r["strike"])
                dcall.append(r["call_oi"] - p["call_oi"])
                dput.append(-(r["put_oi"] - p["put_oi"]))

        if dxs:
            dbw = _bar_width(dxs)
            fig.add_trace(go.Bar(
                x=dxs, y=dcall, width=dbw, name="Δ Call",
                visible=False, showlegend=False, marker_line_width=0,
                marker_color=[D_OPEN if v >= 0 else D_CLOSE for v in dcall],
                hovertemplate="<b>%{x}</b>   Δ Call OI  <b>%{y:+}</b><extra></extra>",
            ), row=1, col=1)
            fig.add_trace(go.Bar(
                x=dxs, y=dput, width=dbw, name="Δ Put",
                visible=False, showlegend=False, marker_line_width=0,
                marker_color=[D_CLOSE if v >= 0 else D_OPEN for v in dput],
                customdata=[-v for v in dput],
                hovertemplate="<b>%{x}</b>   Δ Put OI  <b>%{customdata:+}</b><extra></extra>",
            ), row=1, col=1)
        else:
            for _ in range(2):
                fig.add_trace(go.Bar(x=[], y=[], visible=False, showlegend=False,
                                     marker_line_width=0), row=1, col=1)

    head   = data[0]["analytics"]
    center = head.get("futures_px") or head.get("underlying")

    fig.update_layout(
        paper_bgcolor=VOID, plot_bgcolor=SURFACE,
        font=dict(color=MUTED, family=MONO, size=11),
        separators=SEPARATORS,
        barmode="overlay", bargap=0.06, showlegend=False,
        hovermode="x unified", dragmode="pan",
        hoverlabel=dict(bgcolor="#111114", bordercolor=RULE, align="left",
                        font=dict(color=TEXT, family=MONO, size=12)),
        height=660, margin=dict(t=30, b=44, l=68, r=16),
        transition=dict(duration=180, easing="cubic-in-out"),
    )

    spike = dict(showspikes=True, spikecolor="#3A3A42", spikethickness=1,
                 spikedash="solid", spikemode="across", spikesnap="cursor")
    grid  = dict(gridcolor=RULE, gridwidth=1, zerolinecolor="#2E2E35",
                 zerolinewidth=1, showline=False,
                 tickfont=dict(color=FAINT, size=10, family=MONO))

    fig.update_xaxes(**grid, **spike, nticks=16, tickangle=0, tickformat=",d")
    fig.update_yaxes(**grid, **spike, tickformat=",d")
    if center:
        fig.update_xaxes(range=_xrange(center))

    fig.update_yaxes(row=1, col=1, title_text="OPEN INTEREST", title_standoff=10,
                     title_font=dict(color=FAINT, size=9, family=MONO))
    fig.update_yaxes(row=2, col=1, title_text="VOLUME", title_standoff=10,
                     title_font=dict(color=FAINT, size=9, family=MONO),
                     range=[-VOL_CAP, VOL_CAP])
    return fig


# ── Heatmap ──────────────────────────────────────────────────────────────────
def build_heatmap(data):
    data = sorted([d for d in data if d["strikes"]], key=lambda d: d["expiry"])
    if not data:
        return None

    head   = data[0]["analytics"]
    center = head.get("futures_px") or head.get("underlying")

    # Далекі expiry (2028+) — інституційний хедж, з'їдає шкалу кольорів
    try:
        from dateutil.relativedelta import relativedelta
        horizon = data[0]["expiry"] + relativedelta(months=15)
    except ImportError:
        e = data[0]["expiry"]
        horizon = date(e.year + 1, e.month, e.day)
    data = [d for d in data if d["expiry"] <= horizon]
    if not data:
        return None

    p  = ZOOM + 0.02
    lo = center * (1 - p) if center else 0
    hi = center * (1 + p) if center else 10 ** 9

    strikes = sorted({r["strike"] for d in data for r in d["strikes"]
                      if lo <= r["strike"] <= hi})
    if not strikes:
        return None

    idx    = {s: j for j, s in enumerate(strikes)}
    labels = [d["expiry"].strftime("%d %b %Y") for d in data]
    z_call = [[0] * len(strikes) for _ in data]
    z_put  = [[0] * len(strikes) for _ in data]

    for i, d in enumerate(data):
        for r in d["strikes"]:
            j = idx.get(r["strike"])
            if j is not None:
                z_call[i][j] = r["call_oi"]
                z_put[i][j]  = r["put_oi"]

    fig = make_subplots(rows=1, cols=2, horizontal_spacing=0.07,
                        subplot_titles=["CALL OI  ·  опір", "PUT OI  ·  підтримка"])

    fig.add_trace(go.Heatmap(
        x=strikes, y=labels, z=z_call, showscale=False,
        colorscale=[[0, "#08080A"], [0.15, "#232329"], [0.5, "#4A4A53"], [1, ASH]],
        hovertemplate="%{y}<br><b>%{x}</b>   Call OI  <b>%{z}</b><extra></extra>",
    ), row=1, col=1)

    fig.add_trace(go.Heatmap(
        x=strikes, y=labels, z=z_put, showscale=False,
        colorscale=[[0, "#08080A"], [0.15, "#2E2E33"], [0.5, "#8A8A92"], [1, CHALK]],
        hovertemplate="%{y}<br><b>%{x}</b>   Put OI  <b>%{z}</b><extra></extra>",
    ), row=1, col=2)

    fig.update_layout(
        paper_bgcolor=VOID, plot_bgcolor=SURFACE,
        font=dict(color=MUTED, family=MONO, size=10),
        separators=SEPARATORS,
        height=520, margin=dict(t=44, b=42, l=104, r=18),
        hovermode="closest",
        hoverlabel=dict(bgcolor="#111114", bordercolor=RULE,
                        font=dict(color=TEXT, family=MONO, size=12)),
    )
    for ann in fig.layout.annotations:
        ann.font = dict(color=FAINT, size=10, family=MONO)
        ann.y = 1.045

    fig.update_xaxes(gridcolor=RULE, showline=False, nticks=10, tickformat=",d",
                     tickfont=dict(color=FAINT, size=9, family=MONO))
    fig.update_yaxes(gridcolor=RULE, showline=False,
                     tickfont=dict(color=FAINT, size=9, family=MONO))
    return fig


# ── Метадані для JS ──────────────────────────────────────────────────────────
def build_meta(data, prev_lookup, trade_date_str):
    meta = []
    for d in sorted([x for x in data if x["strikes"]], key=lambda x: x["expiry"]):
        a   = d["analytics"]
        und = a.get("futures_px") or a.get("underlying")
        mp  = a.get("max_pain")
        pcr = a.get("pcr")
        ctr = und or mp

        shapes, notes = [], []
        if und:
            shapes.append(dict(type="line", xref="x", yref="paper",
                               x0=und, x1=und, y0=0, y1=1,
                               line=dict(color=SPOT_C, width=1.5)))
            notes.append(dict(x=und, y=1.012, xref="x", yref="paper",
                              text="SPOT " + _sp(und), showarrow=False,
                              font=dict(color=SPOT_C, size=9.5, family=MONO),
                              xanchor="center", yanchor="bottom"))
        if mp:
            shapes.append(dict(type="line", xref="x", yref="paper",
                               x0=mp, x1=mp, y0=0, y1=1,
                               line=dict(color=PAIN_C, width=1.5, dash="4px,4px")))
            notes.append(dict(x=mp, y=0.985, xref="x", yref="paper",
                              text="MAX PAIN " + _sp(mp), showarrow=False,
                              font=dict(color=PAIN_C, size=9.5, family=MONO),
                              xanchor="center", yanchor="top",
                              bgcolor="rgba(0,0,0,0.8)", borderpad=3))

        rows  = d["strikes"]
        c_oi  = sum(r["call_oi"]       for r in rows)
        p_oi  = sum(r["put_oi"]        for r in rows)
        c_vol = sum(r["call_vol"] or 0 for r in rows)
        p_vol = sum(r["put_vol"]  or 0 for r in rows)

        prev   = prev_lookup.get(d["expiry"], {})
        d_call = sum(r["call_oi"] - prev[r["strike"]]["call_oi"]
                     for r in rows if r["strike"] in prev) if prev else 0
        d_put  = sum(r["put_oi"] - prev[r["strike"]]["put_oi"]
                     for r in rows if r["strike"] in prev) if prev else 0

        pull, direction = "—", "flat"
        if und and mp:
            gap = round(mp - und)
            if gap > 0:
                pull, direction = f"↑ {abs(gap)} пт вище", "up"
            elif gap < 0:
                pull, direction = f"↓ {abs(gap)} пт нижче", "down"
            else:
                pull = "на рівні spot"

        meta.append(dict(
            label      = d["expiry"].strftime("%d %b %Y") + " · " + (d["contract_type"] or "—"),
            shapes=shapes, annotations=notes, xrange=_xrange(ctr),
            spot_txt   = _sp(und),
            pain_txt   = _sp(mp),
            pull_txt   = pull,
            pull_dir   = direction,
            pcr        = f"{pcr:.2f}" if pcr else "—",
            vol_pc     = f"{p_vol / c_vol:.2f}" if c_vol else "—",
            trade_date = trade_date_str,
            c_oi=c_oi, p_oi=p_oi, c_vol=c_vol, p_vol=p_vol,
            call_walls = [{"s": r["strike"], "oi": r["call_oi"]}
                          for r in sorted(rows, key=lambda r: r["call_oi"], reverse=True)[:3]],
            put_walls  = [{"s": r["strike"], "oi": r["put_oi"]}
                          for r in sorted(rows, key=lambda r: r["put_oi"], reverse=True)[:3]],
            has_delta  = bool(prev),
            delta_call = d_call,
            delta_put  = d_put,
        ))
    return meta


# ── HTML ─────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DAX Options · EUREX</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --void:#000; --surface:#0A0A0B; --raised:#0E0E10;
  --rule:#1A1A1D; --rule-lit:#26262B;
  --chalk:#FFF; --ash:#6E6E76;
  --text:#E8E8EA; --muted:#6E6E76; --faint:#45454C;
  --spot:#22D3EE; --pain:#E8A317;
  --open:#2DD4A7; --close:#FB7185;
  --mono:'IBM Plex Mono',ui-monospace,'SF Mono','Cascadia Mono',Consolas,monospace;
  --sans:'IBM Plex Sans',-apple-system,'Segoe UI',system-ui,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{background:var(--void);color:var(--text);font-family:var(--sans);
     font-size:14px;line-height:1.45;-webkit-font-smoothing:antialiased}
:focus-visible{outline:1px solid var(--spot);outline-offset:2px}

.masthead{display:flex;align-items:center;gap:14px;padding:9px 20px;
  border-bottom:1px solid var(--rule);font-family:var(--mono);font-size:10px;
  letter-spacing:.14em;text-transform:uppercase;color:var(--faint)}
.masthead .mark{color:var(--text);font-weight:600;letter-spacing:.2em}
.masthead .dot{width:5px;height:5px;border-radius:50%;background:var(--spot);
  box-shadow:0 0 6px var(--spot);flex:none}
.masthead .grow{flex:1}

.readout{display:grid;grid-template-columns:repeat(4,1fr);
  border-bottom:1px solid var(--rule)}
.metric{padding:16px 20px 15px;border-left:1px solid var(--rule)}
.metric:first-child{border-left:none}
.metric .val{font-family:var(--mono);font-size:29px;font-weight:400;
  letter-spacing:-.015em;line-height:1;color:var(--text);
  font-variant-numeric:tabular-nums}
.metric .key{font-family:var(--mono);font-size:9.5px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--faint);margin-top:9px}
.metric .sub{font-family:var(--mono);font-size:11px;color:var(--muted);
  margin-top:3px;font-variant-numeric:tabular-nums}
.metric.is-spot .val{color:var(--spot)}
.metric.is-pain .val{color:var(--pain)}
.pull{font-weight:500}
.pull.up{color:var(--open)}
.pull.down{color:var(--close)}

.ladders{display:grid;grid-template-columns:1fr 1fr;
  border-bottom:1px solid var(--rule)}
.ladder{padding:11px 20px 12px;border-left:1px solid var(--rule)}
.ladder:first-child{border-left:none}
.ladder h2{font-family:var(--mono);font-size:9.5px;font-weight:400;
  letter-spacing:.16em;text-transform:uppercase;color:var(--faint);margin-bottom:7px}
.rung{position:relative;display:flex;justify-content:space-between;align-items:center;
  padding:3px 7px;margin-bottom:2px;font-family:var(--mono);font-size:12px;
  overflow:hidden;font-variant-numeric:tabular-nums}
.rung .fill{position:absolute;inset:0 auto 0 0;z-index:0;
  transition:width .32s cubic-bezier(.4,0,.2,1)}
.rung.call .fill{background:rgba(110,110,118,.30)}
.rung.put .fill{background:rgba(255,255,255,.13)}
.rung .k,.rung .v{position:relative;z-index:1}
.rung .k{color:var(--text);letter-spacing:.04em}
.rung .v{color:var(--muted);font-size:11px}

.bar{display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:10px 20px;
  border-bottom:1px solid var(--rule)}
.seg{display:flex;border:1px solid var(--rule-lit);border-radius:3px;overflow:hidden}
.seg button{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;
  text-transform:uppercase;padding:6px 15px;background:transparent;color:var(--muted);
  border:none;border-left:1px solid var(--rule-lit);cursor:pointer;
  transition:background .15s,color .15s}
.seg button:first-child{border-left:none}
.seg button:hover:not(:disabled):not(.on){background:#141417;color:var(--text)}
.seg button.on{background:var(--text);color:var(--void);font-weight:600}
.seg button:disabled{color:#2C2C31;cursor:not-allowed}

select{font-family:var(--mono);font-size:11px;color:var(--text);
  background:var(--raised);border:1px solid var(--rule-lit);border-radius:3px;
  padding:6px 10px;cursor:pointer;outline:none;max-width:250px}
select:hover{border-color:#38383F}

.ckey{display:flex;gap:14px;align-items:center;font-family:var(--mono);font-size:10px}
.ckey span{display:flex;gap:6px;align-items:center;color:var(--muted)}
.ckey i{width:9px;height:9px;display:block;flex:none}
.grow{flex:1}

.tape{display:flex;flex-wrap:wrap;border-bottom:1px solid var(--rule);
  font-family:var(--mono);font-size:11px;font-variant-numeric:tabular-nums}
.tape div{padding:7px 18px;border-left:1px solid var(--rule);color:var(--faint);
  letter-spacing:.05em}
.tape div:first-child{border-left:none}
.tape b{color:var(--text);font-weight:500;margin-left:6px}

.note{font-family:var(--mono);font-size:10.5px;color:var(--faint);
  padding:9px 20px 0;min-height:17px;letter-spacing:.03em}

.stage{padding:2px 6px 20px}
#heat-stage{display:none}
.js-plotly-plot .plotly .modebar{background:transparent!important;opacity:.3;
  transition:opacity .2s}
.js-plotly-plot:hover .plotly .modebar{opacity:1}
.modebar-btn path{fill:#6E6E76!important}
.modebar-btn:hover path{fill:#E8E8EA!important}

@media (max-width:900px){
  .readout{grid-template-columns:1fr 1fr}
  .metric{border-top:1px solid var(--rule)}
  .metric:nth-child(-n+2){border-top:none}
  .metric:nth-child(odd){border-left:none}
  .ladders{grid-template-columns:1fr}
  .ladder:last-child{border-left:none;border-top:1px solid var(--rule)}
  .metric .val{font-size:24px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>
</head>
<body>

<header class="masthead">
  <span class="dot"></span>
  <span class="mark">DAX Options</span>
  <span>Eurex · settlement</span>
  <span class="grow"></span>
  <span id="m-date">—</span>
</header>

<section class="readout">
  <div class="metric is-spot">
    <div class="val" id="m-spot">—</div>
    <div class="key">Spot</div>
    <div class="sub">GER40 · Xetra cash</div>
  </div>
  <div class="metric is-pain">
    <div class="val" id="m-pain">—</div>
    <div class="key">Max pain</div>
    <div class="sub">тягне <span class="pull" id="m-pull">—</span></div>
  </div>
  <div class="metric">
    <div class="val" id="m-pcr">—</div>
    <div class="key">Put / Call OI</div>
    <div class="sub" id="m-pcr-sub">—</div>
  </div>
  <div class="metric">
    <div class="val" id="m-vpc">—</div>
    <div class="key">Put / Call Volume</div>
    <div class="sub" id="m-vpc-sub">—</div>
  </div>
</section>

<section class="ladders">
  <div class="ladder">
    <h2>Опір · найбільший Call OI</h2>
    <div id="l-call"></div>
  </div>
  <div class="ladder">
    <h2>Підтримка · найбільший Put OI</h2>
    <div id="l-put"></div>
  </div>
</section>

<div class="bar">
  <div class="seg" role="group" aria-label="Режим графіка">
    <button id="b-oi" class="on" onclick="setMode('oi')">OI + Vol</button>
    <button id="b-gex" onclick="setMode('gex')">GEX</button>
    <button id="b-delta" onclick="setMode('delta')">&Delta; OI</button>
    <button id="b-heat" onclick="setMode('heat')">Heatmap</button>
  </div>
  <div class="ckey" id="colorkey"></div>
  <span class="grow"></span>
  <select id="pick" onchange="setExpiry(this.selectedIndex)" aria-label="Експірація">EXPIRY_OPTIONS</select>
</div>

<div class="tape">
  <div>Put OI<b id="t-poi">—</b></div>
  <div>Call OI<b id="t-coi">—</b></div>
  <div>Put Vol<b id="t-pv">—</b></div>
  <div>Call Vol<b id="t-cv">—</b></div>
  <div>&Delta; Call OI<b id="t-dc">—</b></div>
  <div>&Delta; Put OI<b id="t-dp">—</b></div>
</div>

<div class="note" id="note"></div>

<div class="stage" id="main-stage">PLOTLY_DIV</div>
<div class="stage" id="heat-stage">HEATMAP_DIV</div>

<script>
var META = EXPIRY_META_JSON;
var VOLCAP = VOLCAPVALUE;
var N = META.length, T = 7;
var curE = 0, curM = 'oi';
var HAS_DELTA = META.some(function(m){ return m.has_delta; });

if(!HAS_DELTA) document.getElementById('b-delta').disabled = true;

function num(n){
  if(n === null || n === undefined) return '—';
  return Math.round(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, '\u2009');
}
function signed(n){
  if(!n) return '0';
  return (n > 0 ? '+' : '\u2212') + num(Math.abs(n));
}

function visibility(){
  var v = new Array(N * T).fill(false), o = curE * T;
  v[o+2] = v[o+3] = true;                      // Volume — завжди
  if(curM === 'oi')         { v[o] = v[o+1] = true; }
  else if(curM === 'gex')   { v[o+4] = true; }
  else if(curM === 'delta') { v[o+5] = v[o+6] = true; }
  return v;
}

var KEYS = {
  oi:    [['Calls','var(--ash)'], ['Puts','var(--chalk)']],
  gex:   [['лонг гамма · гасить рух','var(--spot)'], ['шорт гамма · підсилює','var(--close)']],
  delta: [['позиції відкрито','var(--open)'], ['позиції закрито','var(--close)']],
  heat:  [['Calls','var(--ash)'], ['Puts','var(--chalk)']]
};

function drawKey(){
  document.getElementById('colorkey').innerHTML = KEYS[curM].map(function(k){
    return '<span><i style="background:' + k[1] + '"></i>' + k[0] + '</span>';
  }).join('');
}

function drawLadder(rows, id, kind){
  var el = document.getElementById(id);
  if(!rows || !rows.length){
    el.innerHTML = '<div class="rung"><span class="k" style="color:var(--faint)">немає даних</span></div>';
    return;
  }
  var peak = rows[0].oi || 1;
  el.innerHTML = rows.map(function(r){
    var w = Math.max(4, Math.round(r.oi / peak * 92));
    return '<div class="rung ' + kind + '">'
         +   '<span class="fill" style="width:' + w + '%"></span>'
         +   '<span class="k">' + num(r.s) + '</span>'
         +   '<span class="v">' + num(r.oi) + '</span>'
         + '</div>';
  }).join('');
}

var NOTES = {
  gex:   'Позитивний GEX — маркет-мейкери гасять рух і рівень тримає. Негативний — хеджування підсилює рух у той самий бік.',
  delta: 'Зміна відкритого інтересу проти попередньої сесії. Зростання OI на страйку означає, що рівень став вагомішим для ринку.',
  heat:  'Вертикальні смуги — страйки, що тримають OI одразу на кількох експіраціях. Це найстійкіші рівні.'
};

function render(){
  var m = META[curE];
  var heat = (curM === 'heat');

  document.getElementById('main-stage').style.display = heat ? 'none' : 'block';
  document.getElementById('heat-stage').style.display = heat ? 'block' : 'none';

  if(heat){
    var hd = document.getElementById('heatmap');
    if(hd) Plotly.Plots.resize(hd);
  } else {
    var gd = document.getElementById('chart');
    var title = curM === 'delta' ? 'Δ OPEN INTEREST'
              : curM === 'gex'   ? 'GEX  ·  M €'
              : 'OPEN INTEREST';
    /* Двома викликами, не Plotly.update: у поєднаному виклику зміна
       visible разом з xaxis.range оновлює layout, але не _fullLayout —
       рендер бере другий і вісь лишається на старому діапазоні. */
    /* shared_xaxes робить xaxis підпорядкованою до xaxis2 (matches),
       тож задаємо обидві — інакше майстер повертає старий діапазон. */
    Plotly.restyle(gd, { visible: visibility() }).then(function(){
      return Plotly.relayout(gd, {
        shapes: m.shapes,
        annotations: m.annotations,
        'xaxis.range':  m.xrange,
        'xaxis2.range': m.xrange,
        'yaxis.title.text': title
      });
    });
  }

  document.getElementById('m-date').textContent = m.trade_date;
  document.getElementById('m-spot').textContent = m.spot_txt;
  document.getElementById('m-pain').textContent = m.pain_txt;
  var pull = document.getElementById('m-pull');
  pull.textContent = m.pull_txt;
  pull.className = 'pull ' + m.pull_dir;

  document.getElementById('m-pcr').textContent     = m.pcr;
  document.getElementById('m-pcr-sub').textContent = num(m.p_oi) + ' P  /  ' + num(m.c_oi) + ' C';
  document.getElementById('m-vpc').textContent     = m.vol_pc;
  document.getElementById('m-vpc-sub').textContent = num(m.p_vol) + ' P  /  ' + num(m.c_vol) + ' C';

  document.getElementById('t-poi').textContent = num(m.p_oi);
  document.getElementById('t-coi').textContent = num(m.c_oi);
  document.getElementById('t-pv').textContent  = num(m.p_vol);
  document.getElementById('t-cv').textContent  = num(m.c_vol);
  document.getElementById('t-dc').textContent  = m.has_delta ? signed(m.delta_call) : '—';
  document.getElementById('t-dp').textContent  = m.has_delta ? signed(m.delta_put)  : '—';

  drawLadder(m.call_walls, 'l-call', 'call');
  drawLadder(m.put_walls,  'l-put',  'put');
  drawKey();

  document.getElementById('note').textContent = NOTES[curM]
    || ('Puts вниз — підтримка. Calls вгору — опір. Шкала Volume зафіксована на ±'
        + num(VOLCAP) + ', далі видно скролом.');
}

function setMode(mode){
  curM = mode;
  [['oi','b-oi'],['gex','b-gex'],['delta','b-delta'],['heat','b-heat']].forEach(function(p){
    document.getElementById(p[1]).classList.toggle('on', curM === p[0]);
  });
  render();
}

function setExpiry(i){ curE = i; render(); }

var booted = false;
function boot(){ render(); booted = true; }
var chartEl = document.getElementById('chart');
if(chartEl && chartEl.on){
  chartEl.on('plotly_afterplot', function(){ if(!booted) setTimeout(boot, 60); });
}
setTimeout(function(){ if(!booted) boot(); }, 500);
setTimeout(function(){ booted = false; boot(); }, 1600);
window.addEventListener('resize', function(){
  var el = document.getElementById(curM === 'heat' ? 'heatmap' : 'chart');
  if(el) Plotly.Plots.resize(el);
});
</script>
</body>
</html>"""


def build_html(data, prev_lookup, trade_date_str):
    data = sorted([d for d in data if d["strikes"]], key=lambda d: d["expiry"])
    fig  = build_figure(data, prev_lookup)
    meta = build_meta(data, prev_lookup, trade_date_str)

    cfg = {"scrollZoom": True, "displayModeBar": True, "displaylogo": False,
           "modeBarButtonsToRemove": ["select2d", "lasso2d", "autoScale2d",
                                      "toggleSpikelines"]}

    main_div = fig.to_html(full_html=False, include_plotlyjs=PLOTLY_JS,
                           div_id="chart", config=cfg)

    heat = build_heatmap(data)
    heat_div = (heat.to_html(full_html=False, include_plotlyjs=False, div_id="heatmap",
                             config={"scrollZoom": True, "displayModeBar": False})
                if heat else
                "<div style='color:#45454C;padding:44px;text-align:center;"
                "font-family:monospace;font-size:12px'>Недостатньо даних для heatmap</div>")

    opts = "\n".join('<option value="%d">%s</option>' % (i, m["label"])
                     for i, m in enumerate(meta))

    return (HTML
            .replace("EXPIRY_OPTIONS",   opts)
            .replace("PLOTLY_DIV",       main_div)
            .replace("HEATMAP_DIV",      heat_div)
            .replace("VOLCAPVALUE",      str(VOL_CAP))
            .replace("EXPIRY_META_JSON", json.dumps(meta, ensure_ascii=False)))


# ── Точка входу ──────────────────────────────────────────────────────────────
def main():
    init_db()
    asset = "DAX"

    print("\n  Перевіряю базу даних…")
    trade_date, db_data = load_latest(asset)

    provider = EurexProvider(stats_id="70044", asset=asset)
    on_eurex = provider._latest_trade_date()
    print("  Eurex: %s   БД: %s" % (on_eurex or "НЕДОСТУПНИЙ", trade_date or "—"))

    if on_eurex is None:
        # Важливо не сплутати з «немає нової сесії»: там усе гаразд, а тут
        # ми просто не змогли спитати. Пишемо голосно, щоб у логах збірки
        # було видно справжню причину застряглої дати.
        print("  ⚠ Не вдалось опитати Eurex — сторінка буде зібрана "
              "з того, що вже є в базі")
        if not db_data:
            print("  Даних немає взагалі. Виходжу.")
            return
        fetch = False
    elif db_data and trade_date >= on_eurex:
        print("  Дані за %s актуальні" % trade_date)
        fetch = False
    else:
        print("  Нова сесія %s — завантажую" % on_eurex)
        fetch = True

    if fetch:
        chains = [c for c in provider.fetch() if c.strikes]
        if not chains:
            print("  Eurex не повернув даних. Перевір з'єднання.")
            return
        save_chains(chains)
        trade_date = chains[0].trade_date

    print("  Отримую spot Xetra DAX…")
    spot = fetch_dax_spot(trade_date)
    print("  Spot: %s" % f"{spot:,.1f}" if spot else "  Yahoo недоступний — put-call parity")

    _, db_data = load_latest(asset)

    if fetch or any(not d["analytics"] for d in db_data):
        print("  Рахую Max Pain, P/C, GEX…")
        for d in db_data:
            rows = [OptionStrikeRaw(
                strike=r["strike"], call_open_interest=r["call_oi"],
                put_open_interest=r["put_oi"], call_volume=r.get("call_vol"),
                put_volume=r.get("put_vol"), call_settlement=r.get("call_settle"),
                put_settlement=r.get("put_settle"),
            ) for r in d["strikes"]]

            mp  = calc_max_pain(rows)
            pcr = calc_put_call_ratio(rows)
            und = estimate_underlying_parity(rows)
            ref = spot or und

            gex_rows = calc_gex(rows, ref, trade_date, d["expiry"]) if ref else []
            total    = round(sum(g["gex"] for g in gex_rows) / 1e6, 2) if gex_rows else None

            save_analytics(d["chain_id"], mp, pcr, und, spot, total)
            if gex_rows:
                save_gex(d["chain_id"], gex_rows)

            d["analytics"] = {"max_pain": mp, "pcr": pcr, "underlying": und,
                              "futures_px": spot, "total_gex": total}
            d["gex_data"] = gex_rows

    prune_sessions(asset, keep=20)   # база живе між запусками — не даємо рости

    prev_date, prev_chains = load_previous(asset)
    prev_lookup = {c["expiry"]: c["strikes"] for c in prev_chains}
    print("  Δ OI проти %s" % prev_date if prev_date
          else "  Δ OI недоступний — потрібна друга сесія в базі")

    total_strikes = sum(len(d["strikes"]) for d in db_data)
    print("\n  Рендер: %d експірацій, %d страйків" % (len(db_data), total_strikes))

    html = build_html(db_data, prev_lookup, trade_date.strftime("%d %b %Y").upper())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out  = OUT_DIR / "dax_options.html"
    out.write_text(html, encoding="utf-8")
    print("  %s готовий%s\n" % (out.name, "" if HEADLESS else " — відкриваю браузер"))
    if not HEADLESS:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
