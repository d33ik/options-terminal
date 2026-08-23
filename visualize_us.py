"""
US Index Options Terminal — SPX та NDX.

Запуск:  python visualize_us.py   (або подвійний клік на запустити-us.bat)

Дані: публічний фід Cboe (безкоштовний, без ключа, затримка ~15 хв).
База: us_options.db — окремо від DAX, нічого не перемішується.
"""

import sys, logging, webbrowser, json
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).parent / "src"))

import plotly.graph_objects as go
from plotly.subplots import make_subplots

import database
database.DB_PATH = Path(__file__).parent / "us_options.db"   # окрема база

from cboe_provider import CboeProvider, MULTIPLIER
from analytics     import calc_max_pain, calc_put_call_ratio
from gex           import calc_gex_from_greeks
from database      import (init_db, save_chains, save_analytics, save_gex,
                           load_latest, load_previous, prune_sessions)
from models        import OptionStrikeRaw

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



# ── Палітра (та сама мова, що й у DAX-терміналі) ─────────────────────────────
VOID, SURFACE, RULE = "#000000", "#0A0A0B", "#1A1A1D"
CHALK, ASH          = "#FFFFFF", "#6E6E76"     # puts / calls
SPOT_C, PAIN_C      = "#22D3EE", "#E8A317"
GEX_POS, GEX_NEG    = "#22D3EE", "#FB7185"
D_OPEN, D_CLOSE     = "#2DD4A7", "#FB7185"
TEXT, MUTED, FAINT  = "#E8E8EA", "#6E6E76", "#45454C"

ASSETS       = ["SPX", "NDX"]
MAX_EXPIRIES = 10        # у SPX експірації щоденні — без ліміту HTML роздувається
ZOOM         = 0.10      # ±10 %: у US-індексів страйки густіші, ніж у DAX
SEPARATORS   = ". "

MONO = "'IBM Plex Mono', ui-monospace, 'SF Mono', Consolas, monospace"

TRACES_PER_SERIES = 7    # call_oi, put_oi, call_vol, put_vol, gex, d_call, d_put


def _sp(v, f=",.0f"):
    return f"{v:{f}}".replace(",", "\u2009") if v is not None else "—"

def _xrange(c, p=ZOOM):
    return [c * (1 - p), c * (1 + p)] if c else None

def _filter(rows, center, p=ZOOM + 0.03):
    if not center:
        return rows
    lo, hi = center * (1 - p), center * (1 + p)
    keep = [r for r in rows if lo <= r["strike"] <= hi]
    return keep if len(keep) >= 5 else rows

def _bar_width(xs):
    if len(xs) < 2:
        return 5
    return sorted({round(xs[i + 1] - xs[i]) for i in range(len(xs) - 1)})[0] * 0.82

def _nice(x: float) -> int:
    """Округлення вгору до «читабельного» числа: 1, 1.5, 2, 2.5, 3, 4, 5, 7.5."""
    if x <= 0:
        return 10
    import math
    e = math.floor(math.log10(x))
    base = 10 ** e
    for m in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if x <= m * base:
            return int(round(m * base))
    return int(10 * base)


def _cap(values, pct: float = 0.95, mult: float = 1.15, floor: int = 10) -> int:
    """
    Стеля осі за перцентилем, а не за максимумом.

    Один-два величезні страйки (далекі хеджі, роловані позиції) інакше
    розчавлюють шкалу і решта барів перетворюється на писк біля нуля.
    Викиди нікуди не зникають — вони впираються в стелю, а решту стає
    видно. Повний розмах доступний скролом.
    """
    nz = sorted(v for v in values if v and v > 0)
    if not nz:
        return floor
    idx = min(len(nz) - 1, int(len(nz) * pct))
    return max(floor, _nice(nz[idx] * mult))


# ─────────────────────────────────────────────────────────────────────────────
# Фігура: усі серії (актив × експірація) в одному полотні
# ─────────────────────────────────────────────────────────────────────────────
def build_figure(series: list[dict]) -> go.Figure:
    """
    series — плаский список, по одному запису на (актив, експірація).
    Кожен додає рівно TRACES_PER_SERIES трейсів; базовий індекс кладемо
    в сам запис, щоб JS не рахував зсуви й не помилявся.
    """
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.62, 0.38], vertical_spacing=0.025)

    for i, s in enumerate(series):
        s.setdefault("base", i * TRACES_PER_SERIES)
        rows   = s["rows"]
        xs     = [r["strike"] for r in rows]
        bw     = _bar_width(xs)
        first  = (i == 0)
        base   = dict(marker_line_width=0, visible=first, showlegend=False)

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

        # ── GEX: у мільярдах доларів на 1 % руху ────────────────────────────
        grows = _filter(s["gex"], s["center"]) if s["gex"] else []
        gx = [g["strike"] for g in grows]
        gy = [g["gex"] / 1e9 for g in grows]
        giv = [
            (f"IV  {g['call_iv']:.1f}% C / {g['put_iv']:.1f}% P"
             if g["call_iv"] and g["put_iv"] else "")
            for g in grows
        ]
        fig.add_trace(go.Bar(
            x=gx, y=gy, width=(_bar_width(gx) if gx else 5),
            name="GEX", visible=False, showlegend=False, marker_line_width=0,
            marker_color=[GEX_POS if v >= 0 else GEX_NEG for v in gy],
            customdata=giv,
            hovertemplate="<b>%{x}</b>   GEX  <b>%{y:.3f}B $</b><br>%{customdata}<extra></extra>",
        ), row=1, col=1)

        # ── Δ OI ────────────────────────────────────────────────────────────
        prev = s["prev"]
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

    head = series[0]
    fig.update_layout(
        paper_bgcolor=VOID, plot_bgcolor=SURFACE,
        font=dict(color=MUTED, family=MONO, size=11),
        separators=SEPARATORS,
        barmode="overlay", bargap=0.06, showlegend=False,
        hovermode="x unified", dragmode="pan",
        hoverlabel=dict(bgcolor="#111114", bordercolor=RULE, align="left",
                        font=dict(color=TEXT, family=MONO, size=12)),
        height=660, margin=dict(t=30, b=44, l=76, r=16),
        transition=dict(duration=180, easing="cubic-in-out"),
    )

    spike = dict(showspikes=True, spikecolor="#3A3A42", spikethickness=1,
                 spikedash="solid", spikemode="across", spikesnap="cursor")
    grid  = dict(gridcolor=RULE, gridwidth=1, zerolinecolor="#2E2E35",
                 zerolinewidth=1, showline=False,
                 tickfont=dict(color=FAINT, size=10, family=MONO))

    # X — цілі страйки. Y — БЕЗ ",d": у режимі GEX значення дробові
    # (мільярди), і цілочисельний формат схлопнув би їх у «0, 0, -1, -1».
    fig.update_xaxes(**grid, **spike, nticks=16, tickformat=",d")
    fig.update_yaxes(**grid, **spike, separatethousands=True)
    fig.update_xaxes(range=head["xrange"])
    fig.update_yaxes(row=1, col=1, title_text="OPEN INTEREST", title_standoff=10,
                     title_font=dict(color=FAINT, size=9, family=MONO))
    fig.update_yaxes(row=1, col=1, range=[-head["oi_put"], head["oi_call"]])
    fig.update_yaxes(row=2, col=1, title_text="VOLUME", title_standoff=10,
                     title_font=dict(color=FAINT, size=9, family=MONO),
                     range=[-head["vol_put"], head["vol_call"]])
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# Heatmap на актив
# ─────────────────────────────────────────────────────────────────────────────
def build_heatmap(series: list[dict]):
    series = [s for s in series if s["rows"]]
    if not series:
        return None

    center = series[0]["center"]
    lo = center * (1 - ZOOM - 0.02) if center else 0
    hi = center * (1 + ZOOM + 0.02) if center else 10 ** 9

    strikes = sorted({r["strike"] for s in series for r in s["rows"]
                      if lo <= r["strike"] <= hi})
    if not strikes:
        return None

    idx    = {k: j for j, k in enumerate(strikes)}
    labels = [s["expiry"].strftime("%d %b %Y") for s in series]
    z_call = [[0] * len(strikes) for _ in series]
    z_put  = [[0] * len(strikes) for _ in series]

    for i, s in enumerate(series):
        for r in s["rows"]:
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
        height=500, margin=dict(t=44, b=42, l=104, r=18),
        hovermode="closest",
        hoverlabel=dict(bgcolor="#111114", bordercolor=RULE,
                        font=dict(color=TEXT, family=MONO, size=12)),
    )
    for ann in fig.layout.annotations:
        ann.font = dict(color=FAINT, size=10, family=MONO)
        ann.y = 1.05
    fig.update_xaxes(gridcolor=RULE, showline=False, nticks=10, tickformat=",d",
                     tickfont=dict(color=FAINT, size=9, family=MONO))
    fig.update_yaxes(gridcolor=RULE, showline=False,
                     tickfont=dict(color=FAINT, size=9, family=MONO))
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# Метадані серії для JS
# ─────────────────────────────────────────────────────────────────────────────
def build_meta(series: list[dict]) -> list[dict]:
    meta = []
    for s in series:
        spot   = s["spot"]
        mp     = s["max_pain"]
        rows   = s["rows"]

        shapes, notes = [], []
        if spot:
            shapes.append(dict(type="line", xref="x", yref="paper",
                               x0=spot, x1=spot, y0=0, y1=1,
                               line=dict(color=SPOT_C, width=1.5)))
            notes.append(dict(x=spot, y=1.012, xref="x", yref="paper",
                              text="SPOT " + _sp(spot), showarrow=False,
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

        c_oi  = sum(r["call_oi"]       for r in rows)
        p_oi  = sum(r["put_oi"]        for r in rows)
        c_vol = sum(r["call_vol"] or 0 for r in rows)
        p_vol = sum(r["put_vol"]  or 0 for r in rows)

        prev   = s["prev"]
        d_call = sum(r["call_oi"] - prev[r["strike"]]["call_oi"]
                     for r in rows if r["strike"] in prev) if prev else 0
        d_put  = sum(r["put_oi"] - prev[r["strike"]]["put_oi"]
                     for r in rows if r["strike"] in prev) if prev else 0

        pull, direction = "—", "flat"
        if spot and mp:
            gap = round(mp - spot)
            if gap > 0:   pull, direction = f"↑ {abs(gap)} пт вище", "up"
            elif gap < 0: pull, direction = f"↓ {abs(gap)} пт нижче", "down"
            else:         pull = "на рівні spot"

        total_gex = sum(g["gex"] for g in s["gex"]) / 1e9 if s["gex"] else None

        meta.append(dict(
            asset      = s["asset"],
            base       = s["base"],
            label      = s["expiry"].strftime("%d %b %Y") + " · " + (s["contract_type"] or "—"),
            spot_txt   = _sp(spot),
            pain_txt   = _sp(mp),
            pull_txt   = pull,
            pull_dir   = direction,
            pcr        = f"{s['pcr']:.2f}" if s["pcr"] else "—",
            vol_pc     = f"{p_vol / c_vol:.2f}" if c_vol else "—",
            gex_txt    = (f"{total_gex:+.2f}B" if total_gex is not None else "—"),
            gex_dir    = ("up" if (total_gex or 0) >= 0 else "down"),
            stamp      = s["stamp"],
            shapes=shapes, annotations=notes, xrange=s["xrange"],
            oi_call=s["oi_call"], oi_put=s["oi_put"],
            vol_call=s["vol_call"], vol_put=s["vol_put"],
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


HTML = r"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SPX · NDX Options — Cboe</title>
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
.masthead .dot{width:5px;height:5px;border-radius:50%;background:var(--spot);
  box-shadow:0 0 6px var(--spot);flex:none}
.masthead .grow{flex:1}

/* Перемикач активу — головний елемент шапки */
.assets{display:flex;gap:0;border:1px solid var(--rule-lit);border-radius:3px;overflow:hidden}
.assets button{font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.14em;
  padding:5px 18px;background:transparent;color:var(--muted);border:none;
  border-left:1px solid var(--rule-lit);cursor:pointer;transition:background .15s,color .15s}
.assets button:first-child{border-left:none}
.assets button:hover:not(.on){background:#141417;color:var(--text)}
.assets button.on{background:var(--spot);color:#000}

.readout{display:grid;grid-template-columns:repeat(5,1fr);
  border-bottom:1px solid var(--rule)}
.metric{padding:16px 20px 15px;border-left:1px solid var(--rule)}
.metric:first-child{border-left:none}
.metric .val{font-family:var(--mono);font-size:27px;font-weight:400;
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
.gexval.up{color:var(--spot)}
.gexval.down{color:var(--close)}

.ladders{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid var(--rule)}
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
.hidden{display:none}
.js-plotly-plot .plotly .modebar{background:transparent!important;opacity:.3;
  transition:opacity .2s}
.js-plotly-plot:hover .plotly .modebar{opacity:1}
.modebar-btn path{fill:#6E6E76!important}
.modebar-btn:hover path{fill:#E8E8EA!important}

@media (max-width:1100px){ .readout{grid-template-columns:1fr 1fr 1fr} }
@media (max-width:820px){
  .readout{grid-template-columns:1fr 1fr}
  .metric{border-top:1px solid var(--rule)}
  .metric:nth-child(-n+2){border-top:none}
  .metric:nth-child(odd){border-left:none}
  .ladders{grid-template-columns:1fr}
  .ladder:last-child{border-left:none;border-top:1px solid var(--rule)}
  .metric .val{font-size:22px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>
</head>
<body>

<header class="masthead">
  <span class="dot"></span>
  <div class="assets" id="assetbar" role="group" aria-label="Актив"></div>
  <span>Cboe · delayed</span>
  <span class="grow"></span>
  <span id="m-stamp">—</span>
</header>

<section class="readout">
  <div class="metric is-spot">
    <div class="val" id="m-spot">—</div>
    <div class="key">Spot</div>
    <div class="sub" id="m-index">—</div>
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
  <div class="metric">
    <div class="val"><span class="gexval" id="m-gex">—</span></div>
    <div class="key">Net GEX</div>
    <div class="sub">доларів на 1 % руху</div>
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
  <select id="pick" onchange="setSeries(this.value)" aria-label="Експірація"></select>
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
<div class="stage hidden" id="heat-stage">HEATMAP_DIVS</div>

<script>
var META      = EXPIRY_META_JSON;
var ASSETLIST = ASSETLIST_JSON;
var NTRACES   = NTRACES_VALUE;
var T         = 7;

var curAsset = ASSETLIST[0];
var curIdx   = 0;          // індекс у META
var curMode  = 'oi';

function num(n){
  if(n === null || n === undefined) return '—';
  return Math.round(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, '\u2009');
}
function signed(n){
  if(!n) return '0';
  return (n > 0 ? '+' : '\u2212') + num(Math.abs(n));
}
function seriesOf(asset){
  return META.map(function(m,i){ return {m:m,i:i}; })
             .filter(function(x){ return x.m.asset === asset; });
}

/* Кнопки активів */
(function(){
  document.getElementById('assetbar').innerHTML = ASSETLIST.map(function(a,k){
    return '<button id="a-'+a+'" class="'+(k===0?'on':'')+'" onclick="setAsset(\''+a+'\')">'+a+'</button>';
  }).join('');
})();

function visibility(){
  var v = new Array(NTRACES).fill(false);
  var o = META[curIdx].base;
  v[o+2] = v[o+3] = true;                        // Volume — завжди
  if(curMode === 'oi')         { v[o] = v[o+1] = true; }
  else if(curMode === 'gex')   { v[o+4] = true; }
  else if(curMode === 'delta') { v[o+5] = v[o+6] = true; }
  return v;
}

var KEYS = {
  oi:    [['Calls','var(--ash)'], ['Puts','var(--chalk)']],
  gex:   [['лонг гамма · гасить рух','var(--spot)'], ['шорт гамма · підсилює','var(--close)']],
  delta: [['позиції відкрито','var(--open)'], ['позиції закрито','var(--close)']],
  heat:  [['Calls','var(--ash)'], ['Puts','var(--chalk)']]
};

var NOTES = {
  gex:   'Позитивний GEX — маркет-мейкери гасять рух і рівень тримає. Негативний — хеджування підсилює рух у той самий бік.',
  delta: 'Зміна відкритого інтересу проти попереднього запуску. Зростання OI на страйку означає, що рівень став вагомішим.',
  heat:  'Вертикальні смуги — страйки, що тримають OI одразу на кількох експіраціях. Це найстійкіші рівні.'
};

function drawKey(){
  document.getElementById('colorkey').innerHTML = KEYS[curMode].map(function(k){
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

function fillPicker(){
  var list = seriesOf(curAsset);
  document.getElementById('pick').innerHTML = list.map(function(x){
    return '<option value="' + x.i + '">' + x.m.label + '</option>';
  }).join('');
  if(!list.some(function(x){ return x.i === curIdx; })) curIdx = list[0].i;
  document.getElementById('pick').value = String(curIdx);
}

function render(){
  var m = META[curIdx];
  var heat = (curMode === 'heat');

  document.getElementById('main-stage').classList.toggle('hidden', heat);
  document.getElementById('heat-stage').classList.toggle('hidden', !heat);

  ASSETLIST.forEach(function(a){
    var el = document.getElementById('heat-' + a);
    if(el) el.parentElement.classList.toggle('hidden', a !== curAsset);
  });

  if(heat){
    var hd = document.getElementById('heat-' + curAsset);
    if(hd) Plotly.Plots.resize(hd);
  } else {
    var gd = document.getElementById('chart');
    var title = curMode === 'delta' ? 'Δ OPEN INTEREST'
              : curMode === 'gex'   ? 'GEX  ·  B $ / 1%'
              : 'OPEN INTEREST';
    /* Обов'язково двома викликами.
       Plotly.update(visible + xaxis.range) в одному виклику оновлює
       layout, але не _fullLayout — рендер бере другий, і бари малюються
       за діапазоном попереднього активу, тобто за межами полотна.
       Перевірено: restyle → relayout тримає обидва в синхроні. */
    /* shared_xaxes=True створює дві осі X: xaxis (верхній ряд) має
       matches:'x2', тобто підпорядкована майстру xaxis2. Якщо задати
       лише 'xaxis.range', майстер перебиває його назад і бари
       малюються за діапазоном попереднього активу. Задаємо обидві. */
    var lay = {
      shapes: m.shapes,
      annotations: m.annotations,
      'xaxis.range':  m.xrange,
      'xaxis2.range': m.xrange,
      'yaxis.title.text': title,
      /* Volume завжди за своєю стелею */
      'yaxis2.range': [-m.vol_put, m.vol_call],
      'yaxis2.autorange': false
    };
    /* Верхня панель: у режимі OI тримаємо стелю за перцентилем,
       у GEX та Δ OI масштаб зовсім інший — віддаємо autorange. */
    if(curMode === 'oi'){
      lay['yaxis.range'] = [-m.oi_put, m.oi_call];
      lay['yaxis.autorange'] = false;
    } else {
      lay['yaxis.autorange'] = true;
    }
    Plotly.restyle(gd, { visible: visibility() }).then(function(){
      return Plotly.relayout(gd, lay);
    });
  }

  document.getElementById('m-stamp').textContent = m.stamp;
  document.getElementById('m-spot').textContent  = m.spot_txt;
  document.getElementById('m-index').textContent = m.asset + ' index · Cboe';
  document.getElementById('m-pain').textContent  = m.pain_txt;
  var pull = document.getElementById('m-pull');
  pull.textContent = m.pull_txt;
  pull.className = 'pull ' + m.pull_dir;

  document.getElementById('m-pcr').textContent     = m.pcr;
  document.getElementById('m-pcr-sub').textContent = num(m.p_oi) + ' P  /  ' + num(m.c_oi) + ' C';
  document.getElementById('m-vpc').textContent     = m.vol_pc;
  document.getElementById('m-vpc-sub').textContent = num(m.p_vol) + ' P  /  ' + num(m.c_vol) + ' C';
  var gx = document.getElementById('m-gex');
  gx.textContent = m.gex_txt;
  gx.className = 'gexval ' + m.gex_dir;

  document.getElementById('t-poi').textContent = num(m.p_oi);
  document.getElementById('t-coi').textContent = num(m.c_oi);
  document.getElementById('t-pv').textContent  = num(m.p_vol);
  document.getElementById('t-cv').textContent  = num(m.c_vol);
  document.getElementById('t-dc').textContent  = m.has_delta ? signed(m.delta_call) : '—';
  document.getElementById('t-dp').textContent  = m.has_delta ? signed(m.delta_put)  : '—';

  drawLadder(m.call_walls, 'l-call', 'call');
  drawLadder(m.put_walls,  'l-put',  'put');
  drawKey();

  var anyDelta = seriesOf(curAsset).some(function(x){ return x.m.has_delta; });
  document.getElementById('b-delta').disabled = !anyDelta;
  if(!anyDelta && curMode === 'delta') setMode('oi');

  document.getElementById('note').textContent = NOTES[curMode]
    || ('Puts вниз — підтримка. Calls вгору — опір. Шкали обрізані за 95-м '
        + 'перцентилем (OI +' + num(m.oi_call) + ' / \u2212' + num(m.oi_put)
        + '), щоб поодинокі величезні страйки не з\'їдали графік — повний '
        + 'розмах видно скролом.');
}

function setAsset(a){
  if(a === curAsset) return;
  curAsset = a;
  ASSETLIST.forEach(function(x){
    document.getElementById('a-' + x).classList.toggle('on', x === a);
  });
  fillPicker();
  render();
}
function setSeries(i){ curIdx = parseInt(i, 10); render(); }
function setMode(mode){
  curMode = mode;
  [['oi','b-oi'],['gex','b-gex'],['delta','b-delta'],['heat','b-heat']].forEach(function(p){
    document.getElementById(p[1]).classList.toggle('on', curMode === p[0]);
  });
  render();
}

var booted = false;
function boot(){ fillPicker(); render(); booted = true; }
var chartEl = document.getElementById('chart');
if(chartEl && chartEl.on){
  chartEl.on('plotly_afterplot', function(){ if(!booted) setTimeout(boot, 60); });
}
setTimeout(function(){ if(!booted) boot(); }, 500);
setTimeout(function(){ booted = false; boot(); }, 1600);
window.addEventListener('resize', function(){
  var el = document.getElementById(curMode === 'heat' ? ('heat-' + curAsset) : 'chart');
  if(el) Plotly.Plots.resize(el);
});
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
def build_html(series: list[dict]) -> str:
    # Базовий індекс трейсів — джерело правди для JS.
    # Проставляємо тут, до обох білдерів, щоб build_meta не залежав
    # від того, чи викликали перед ним build_figure.
    for i, s in enumerate(series):
        s["base"] = i * TRACES_PER_SERIES

    fig  = build_figure(series)
    meta = build_meta(series)

    cfg = {"scrollZoom": True, "displayModeBar": True, "displaylogo": False,
           "modeBarButtonsToRemove": ["select2d", "lasso2d", "autoScale2d",
                                      "toggleSpikelines"]}
    main_div = fig.to_html(full_html=False, include_plotlyjs=PLOTLY_JS,
                           div_id="chart", config=cfg)

    # Один heatmap на актив
    parts, first = [], True
    for asset in ASSETS:
        sub = [s for s in series if s["asset"] == asset]
        hm  = build_heatmap(sub) if sub else None
        inner = (hm.to_html(full_html=False, include_plotlyjs=False,
                            div_id="heat-" + asset,
                            config={"scrollZoom": True, "displayModeBar": False})
                 if hm else
                 f"<div id='heat-{asset}' style='color:#45454C;padding:44px;"
                 f"text-align:center;font-family:monospace;font-size:12px'>"
                 f"Немає даних для {asset}</div>")
        parts.append(f"<div class='{'' if first else 'hidden'}'>{inner}</div>")
        first = False

    assets_present = [a for a in ASSETS if any(s["asset"] == a for s in series)]

    return (HTML
            .replace("PLOTLY_DIV",       main_div)
            .replace("HEATMAP_DIVS",     "\n".join(parts))
            .replace("NTRACES_VALUE",    str(len(series) * TRACES_PER_SERIES))
            .replace("ASSETLIST_JSON",   json.dumps(assets_present))
            .replace("EXPIRY_META_JSON", json.dumps(meta, ensure_ascii=False)))


def collect(asset: str) -> list[dict]:
    """Тягне актив, зберігає в базу, рахує аналітику, повертає серії."""
    provider = CboeProvider(asset, max_expiries=MAX_EXPIRIES)
    chains, spot, ts = provider.fetch()
    chains = [c for c in chains if c.strikes]
    if not chains:
        print(f"  {asset}: даних немає")
        return []

    save_chains(chains)
    trade_date = chains[0].trade_date
    stamp = ts.strftime("%d %b %Y  %H:%M").upper() if ts else trade_date.strftime("%d %b %Y").upper()
    mult  = MULTIPLIER.get(asset, 100.0)

    # Попередній запуск — для Δ OI
    _, prev_chains = load_previous(asset)
    prev_lookup = {c["expiry"]: c["strikes"] for c in prev_chains}

    _, stored = load_latest(asset)
    stored_by_expiry = {d["expiry"]: d for d in stored}

    series = []
    for ch in chains:
        rec = stored_by_expiry.get(ch.expiry)
        if not rec:
            continue

        mp  = calc_max_pain(ch.strikes)
        pcr = calc_put_call_ratio(ch.strikes)
        gex = calc_gex_from_greeks(ch.strikes, spot, mult) if spot else []
        tot = round(sum(g["gex"] for g in gex) / 1e9, 3) if gex else None

        save_analytics(rec["chain_id"], mp, pcr, spot, spot, tot)
        if gex:
            save_gex(rec["chain_id"], gex)

        center = spot or mp
        rows = _filter(rec["strikes"], center)
        series.append(dict(
            asset=asset, expiry=ch.expiry, contract_type=ch.contract_type,
            rows=rows,
            gex=gex, spot=spot, max_pain=mp, pcr=pcr,
            center=center, xrange=_xrange(center),
            prev=prev_lookup.get(ch.expiry, {}),
            stamp=stamp,
            # Шкали рахуємо на кожну серію окремо: тижнева NDX має OI
            # у десятки, місячна SPX — у тисячі. Спільна стеля зробила б
            # одну з них нечитабельною.
            oi_call  = _cap(r["call_oi"]        for r in rows),
            oi_put   = _cap(r["put_oi"]         for r in rows),
            vol_call = _cap(r["call_vol"] or 0  for r in rows),
            vol_put  = _cap(r["put_vol"]  or 0  for r in rows),
        ))

    print(f"  {asset}: {len(series)} експірацій, spot {spot}")
    return series


def main():
    init_db()
    print("\n  Тягну ланцюжки з Cboe…")

    series = []
    for asset in ASSETS:
        series.extend(collect(asset))
        prune_sessions(asset, keep=20)

    if not series:
        print("  Дані не прийшли. Перевір з'єднання.")
        return

    total = sum(len(s["rows"]) for s in series)
    print(f"\n  Рендер: {len(series)} серій, {total} страйків")

    html = build_html(series)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out  = OUT_DIR / "us_options.html"
    out.write_text(html, encoding="utf-8")
    print(f"  {out.name} готовий" + ("" if HEADLESS else " — відкриваю браузер") + "\n")
    if not HEADLESS:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
