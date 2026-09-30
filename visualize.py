"""
DAX Options Terminal — дашборд опціонної аналітики.

Запуск:  python visualize.py   (або подвійний клік на запустити.bat)
"""

import sys, logging, webbrowser, json, os
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).parent / "src"))

import plotly.graph_objects as go

from eurex_provider import EurexProvider
from futures_price  import fetch_dax_spot
from analytics      import calc_max_pain, calc_put_call_ratio, estimate_underlying_parity
from gex            import calc_gex
from database       import (init_db, save_chains, save_analytics, save_gex,
                            load_latest, load_previous, prune_sessions)
from models         import OptionStrikeRaw

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

# ── Режим сервера (вмикається лише змінними оточення в GitHub Actions) ──────
HEADLESS  = bool(os.environ.get("OPTIONS_HEADLESS"))
OUT_DIR   = Path(os.environ.get("OPTIONS_OUT_DIR") or Path(__file__).parent)
PLOTLY_JS = "cdn" if HEADLESS else True

# ── Палітра ──────────────────────────────────────────────────────────────────
VOID, SURFACE, RULE = "#000000", "#0A0A0B", "#1A1A1D"
CHALK, ASH          = "#FFFFFF", "#6E6E76"     # puts / calls
SPOT_C, PAIN_C      = "#22D3EE", "#E8A317"
GEX_POS, GEX_NEG    = "#22D3EE", "#FB7185"
D_OPEN, D_CLOSE     = "#2DD4A7", "#FB7185"
TEXT, MUTED, FAINT  = "#E8E8EA", "#6E6E76", "#45454C"

SEPARATORS = ". "
MONO = "'IBM Plex Mono', ui-monospace, 'SF Mono', Consolas, monospace"

# Рахуємо у СТРАЙКАХ, не у відсотках: сітка нерівномірна, тож однаковий
# відсоток давав то 40 барів, то 300.
KEEP_STRIKES = 140
VIEW_STRIKES = 46

TRACES_PER_SERIES = 7  # call_oi, put_oi, call_vol, put_vol, gex, d_call, d_put


def _sp(v, f=",.0f"):
    return f"{v:{f}}".replace(",", " ") if v is not None else "—"


def _window(rows, center, keep=KEEP_STRIKES):
    """Лишає `keep` страйків, найближчих до центру, у порядку зростання."""
    if not rows:
        return []
    if not center:
        return sorted(rows, key=lambda r: r["strike"])[:keep]
    near = sorted(rows, key=lambda r: abs(r["strike"] - center))[:keep]
    return sorted(near, key=lambda r: r["strike"])


def _cat_pos(strikes, value):
    """
    Позиція ціни на індексній осі — дробовий номер між страйками.

    Вісь індексна (0,1,2…), щоб усі бари вийшли однакової ширини навіть
    там, де крок сітки стрибає. Платою є те, що вертикальні лінії spot і
    max pain не можна поставити за значенням — позицію рахуємо самі.
    """
    if not strikes or value is None:
        return None
    if value <= strikes[0]:
        return 0.0
    if value >= strikes[-1]:
        return float(len(strikes) - 1)
    for i in range(len(strikes) - 1):
        lo, hi = strikes[i], strikes[i + 1]
        if lo <= value <= hi:
            span = hi - lo
            return i + ((value - lo) / span if span else 0.0)
    return float(len(strikes) - 1)


def _view_range(strikes, center, width=VIEW_STRIKES):
    n = len(strikes)
    if n == 0:
        return None
    if n <= width:
        return [-0.5, n - 0.5]
    mid = _cat_pos(strikes, center)
    if mid is None:
        mid = n / 2
    half = width / 2
    lo = max(-0.5, mid - half)
    hi = min(n - 0.5, lo + width)
    lo = max(-0.5, hi - width)
    return [lo, hi]


def _ticks(strikes, every=None):
    n = len(strikes)
    if n == 0:
        return [], []
    if every is None:
        every = max(1, round(n / 26))
    idx = list(range(0, n, every))
    return idx, [_sp(strikes[i]) for i in idx]


def _nice(x):
    if x <= 0:
        return 10
    import math
    e = math.floor(math.log10(x))
    base = 10 ** e
    for m in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if x <= m * base:
            return int(round(m * base))
    return int(10 * base)


def _cap(values, pct=0.97, mult=1.1, floor=10):
    """Стеля осі за перцентилем — поодинокі велетні інакше душать решту."""
    nz = sorted(v for v in values if v and v > 0)
    if not nz:
        return floor
    idx = min(len(nz) - 1, int(len(nz) * pct))
    return max(floor, _nice(nz[idx] * mult))


# ─────────────────────────────────────────────────────────────────────────────
# Головна фігура — ОДНЕ полотно, індексна вісь X
# ─────────────────────────────────────────────────────────────────────────────
def build_figure(series):
    """
    Кожна серія додає TRACES_PER_SERIES трейсів:
      0 call_oi  1 put_oi  2 call_vol  3 put_vol  4 gex  5 d_call  6 d_put

    Вісь X — порядковий номер страйку, а не його значення. Через це всі
    бари однакової ширини навіть там, де крок сітки стрибає з 5 на 50;
    реальні страйки показують підписи. Раніше ширину доводилось брати з
    кроку сітки, і графік виходив із плит упереміш із рисками.

    Полотно одне: Volume переїхав у власну вкладку, тож OI займає всю
    висоту — на двох панелях бари були вдвічі нижчі й гірше читались.
    """
    fig = go.Figure()

    for i, s in enumerate(series):
        s.setdefault("base", i * TRACES_PER_SERIES)
        rows    = s["rows"]
        strikes = [r["strike"] for r in rows]
        xs      = list(range(len(rows)))          # 0,1,2… — рівний крок
        labels  = [_sp(k) for k in strikes]
        shown   = (i == 0)
        base    = dict(marker_line_width=0, visible=shown, showlegend=False,
                       width=0.82, customdata=labels)

        fig.add_trace(go.Bar(
            x=xs, y=[r["call_oi"] for r in rows], name="Call OI",
            marker_color=ASH,
            hovertemplate="<b>%{customdata}</b>   Call OI  <b>%{y}</b><extra></extra>",
            **base,
        ))
        fig.add_trace(go.Bar(
            x=xs, y=[-r["put_oi"] for r in rows], name="Put OI",
            marker_color=CHALK,
            customdata=[[l, r["put_oi"]] for l, r in zip(labels, rows)],
            hovertemplate="<b>%{customdata[0]}</b>   Put OI  <b>%{customdata[1]}</b><extra></extra>",
            **{k: v for k, v in base.items() if k != "customdata"},
        ))
        fig.add_trace(go.Bar(
            x=xs, y=[r["call_vol"] or 0 for r in rows], name="Call Vol",
            marker_color=ASH, opacity=0.85,
            hovertemplate="<b>%{customdata}</b>   Call Vol  <b>%{y}</b><extra></extra>",
            **base,
        ))
        fig.add_trace(go.Bar(
            x=xs, y=[-(r["put_vol"] or 0) for r in rows], name="Put Vol",
            marker_color=CHALK, opacity=0.85,
            customdata=[[l, r["put_vol"] or 0] for l, r in zip(labels, rows)],
            hovertemplate="<b>%{customdata[0]}</b>   Put Vol  <b>%{customdata[1]}</b><extra></extra>",
            **{k: v for k, v in base.items() if k != "customdata"},
        ))

        # ── GEX ──────────────────────────────────────────────────────────────
        gex_by_strike = {g["strike"]: g for g in s["gex"]} if s["gex"] else {}
        gy, gtxt = [], []
        for k, lab in zip(strikes, labels):
            g = gex_by_strike.get(k)
            gy.append((g["gex"] / 1e6) if g else 0.0)
            iv = ""
            if g and g.get("call_iv") and g.get("put_iv"):
                iv = f"IV  {g['call_iv']:.1f}% C / {g['put_iv']:.1f}% P"
            gtxt.append([lab, iv])
        fig.add_trace(go.Bar(
            x=xs, y=gy, name="GEX", width=0.82,
            visible=False, showlegend=False, marker_line_width=0,
            marker_color=[GEX_POS if v >= 0 else GEX_NEG for v in gy],
            customdata=gtxt,
            hovertemplate="<b>%{customdata[0]}</b>   GEX  <b>%{y:.2f}M €</b>"
                          "<br>%{customdata[1]}<extra></extra>",
        ))

        # ── Δ OI ─────────────────────────────────────────────────────────────
        prev = s["prev"]
        dcall, dput = [], []
        for r in rows:
            p = prev.get(r["strike"]) if prev else None
            dcall.append((r["call_oi"] - p["call_oi"]) if p else 0)
            dput.append((-(r["put_oi"] - p["put_oi"])) if p else 0)

        fig.add_trace(go.Bar(
            x=xs, y=dcall, name="Δ Call", width=0.82,
            visible=False, showlegend=False, marker_line_width=0,
            marker_color=[D_OPEN if v >= 0 else D_CLOSE for v in dcall],
            customdata=labels,
            hovertemplate="<b>%{customdata}</b>   Δ Call OI  <b>%{y:+}</b><extra></extra>",
        ))
        fig.add_trace(go.Bar(
            x=xs, y=dput, name="Δ Put", width=0.82,
            visible=False, showlegend=False, marker_line_width=0,
            marker_color=[D_CLOSE if v >= 0 else D_OPEN for v in dput],
            customdata=[[l, -v] for l, v in zip(labels, dput)],
            hovertemplate="<b>%{customdata[0]}</b>   Δ Put OI  <b>%{customdata[1]:+}</b><extra></extra>",
        ))

    head = series[0]

    fig.update_layout(
        paper_bgcolor=VOID, plot_bgcolor=SURFACE,
        font=dict(color=MUTED, family=MONO, size=11),
        separators=SEPARATORS,
        barmode="overlay", bargap=0.12, showlegend=False,
        hovermode="x unified", dragmode="pan",
        hoverlabel=dict(bgcolor="#111114", bordercolor=RULE, align="left",
                        font=dict(color=TEXT, family=MONO, size=12)),
        height=620, margin=dict(t=34, b=52, l=76, r=18),
        # Плавний перехід між станами: Plotly анімує бари й осі замість
        # миттєвої підміни, тож перемикання читається як рух, не як ривок.
        transition=dict(duration=340, easing="cubic-in-out"),
    )

    spike = dict(showspikes=True, spikecolor="#3A3A42", spikethickness=1,
                 spikedash="solid", spikemode="across", spikesnap="cursor")

    tickvals, ticktext = _ticks(head["strikes"])
    fig.update_xaxes(
        gridcolor=RULE, zerolinecolor="#2E2E35", showline=False,
        tickfont=dict(color=FAINT, size=10, family=MONO),
        tickmode="array", tickvals=tickvals, ticktext=ticktext,
        range=head["view"], **spike,
    )
    fig.update_yaxes(
        gridcolor=RULE, zerolinecolor="#2E2E35", zerolinewidth=1, showline=False,
        tickfont=dict(color=FAINT, size=10, family=MONO),
        separatethousands=True, title_standoff=12,
        title_text="OPEN INTEREST",
        title_font=dict(color=FAINT, size=9, family=MONO),
        range=[-head["oi_put"], head["oi_call"]], **spike,
    )
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# Heatmap: expiry × страйк
# ─────────────────────────────────────────────────────────────────────────────
def build_heatmap(series):
    series = [s for s in series if s["rows"]]
    if not series:
        return None

    center = series[0]["center"]
    pool = sorted({r["strike"] for s in series for r in s["rows"]})
    if center and len(pool) > 90:
        pool = sorted(sorted(pool, key=lambda k: abs(k - center))[:90])
    if not pool:
        return None

    idx    = {k: j for j, k in enumerate(pool)}
    labels = [s["expiry"].strftime("%d %b %Y") for s in series]
    z_call = [[0] * len(pool) for _ in series]
    z_put  = [[0] * len(pool) for _ in series]

    for i, s in enumerate(series):
        for r in s["rows"]:
            j = idx.get(r["strike"])
            if j is not None:
                z_call[i][j] = r["call_oi"]
                z_put[i][j]  = r["put_oi"]

    from plotly.subplots import make_subplots
    fig = make_subplots(rows=1, cols=2, horizontal_spacing=0.07,
                        subplot_titles=["CALL OI  ·  опір", "PUT OI  ·  підтримка"])
    xs = [_sp(k) for k in pool]

    fig.add_trace(go.Heatmap(
        x=xs, y=labels, z=z_call, showscale=False,
        colorscale=[[0, "#08080A"], [0.15, "#232329"], [0.5, "#4A4A53"], [1, ASH]],
        hovertemplate="%{y}<br><b>%{x}</b>   Call OI  <b>%{z}</b><extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Heatmap(
        x=xs, y=labels, z=z_put, showscale=False,
        colorscale=[[0, "#08080A"], [0.15, "#2E2E33"], [0.5, "#8A8A92"], [1, CHALK]],
        hovertemplate="%{y}<br><b>%{x}</b>   Put OI  <b>%{z}</b><extra></extra>",
    ), row=1, col=2)

    fig.update_layout(
        paper_bgcolor=VOID, plot_bgcolor=SURFACE,
        font=dict(color=MUTED, family=MONO, size=10),
        separators=SEPARATORS,
        height=520, margin=dict(t=46, b=44, l=108, r=18),
        hovermode="closest",
        hoverlabel=dict(bgcolor="#111114", bordercolor=RULE,
                        font=dict(color=TEXT, family=MONO, size=12)),
    )
    for ann in fig.layout.annotations:
        ann.font = dict(color=FAINT, size=10, family=MONO)
        ann.y = 1.05
    fig.update_xaxes(gridcolor=RULE, showline=False, nticks=12,
                     tickfont=dict(color=FAINT, size=9, family=MONO))
    fig.update_yaxes(gridcolor=RULE, showline=False,
                     tickfont=dict(color=FAINT, size=9, family=MONO))
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# Метадані серії для JS
# ─────────────────────────────────────────────────────────────────────────────
def build_meta(series):
    meta = []
    for i, s in enumerate(series):
        s.setdefault("base", i * TRACES_PER_SERIES)
        rows    = s["rows"]
        strikes = s["strikes"]
        spot    = s["spot"]
        mp      = s["max_pain"]

        # Позиції ліній — в індексах категорій, не в цінах
        spot_x = _cat_pos(strikes, spot)
        pain_x = _cat_pos(strikes, mp)

        shapes, notes = [], []
        if spot_x is not None:
            shapes.append(dict(type="line", xref="x", yref="paper",
                               x0=spot_x, x1=spot_x, y0=0, y1=1,
                               line=dict(color=SPOT_C, width=1.5)))
            notes.append(dict(x=spot_x, y=1.015, xref="x", yref="paper",
                              text="SPOT " + _sp(spot), showarrow=False,
                              font=dict(color=SPOT_C, size=10, family=MONO),
                              xanchor="center", yanchor="bottom"))
        if pain_x is not None:
            shapes.append(dict(type="line", xref="x", yref="paper",
                               x0=pain_x, x1=pain_x, y0=0, y1=1,
                               line=dict(color=PAIN_C, width=1.5, dash="4px,4px")))
            notes.append(dict(x=pain_x, y=0.982, xref="x", yref="paper",
                              text="MAX PAIN " + _sp(mp), showarrow=False,
                              font=dict(color=PAIN_C, size=10, family=MONO),
                              xanchor="center", yanchor="top",
                              bgcolor="rgba(0,0,0,0.8)", borderpad=3))

        c_oi  = sum(r["call_oi"]       for r in rows)
        p_oi  = sum(r["put_oi"]        for r in rows)
        c_vol = sum(r["call_vol"] or 0 for r in rows)
        p_vol = sum(r["put_vol"]  or 0 for r in rows)

        # Коефіцієнти — з тих самих rows, що дають суми поруч,
        # інакше плитка розходиться з надрукованими під нею числами.
        pc_oi  = f"{p_oi / c_oi:.2f}"   if c_oi  else "—"
        pc_vol = f"{p_vol / c_vol:.2f}" if c_vol else "—"

        # ── Δ OI: рахуємо явно, щоб відрізнити «немає з чим порівняти»
        #    від «порівняли, і нічого не змінилось». Раніше обидва випадки
        #    давали порожнє полотно без жодного пояснення.
        prev = s["prev"]
        matched = [r for r in rows if prev and r["strike"] in prev]
        d_call = sum(r["call_oi"] - prev[r["strike"]]["call_oi"] for r in matched)
        d_put  = sum(r["put_oi"]  - prev[r["strike"]]["put_oi"]  for r in matched)
        moves  = [abs(r["call_oi"] - prev[r["strike"]]["call_oi"]) +
                  abs(r["put_oi"]  - prev[r["strike"]]["put_oi"]) for r in matched]
        changed = sum(1 for m in moves if m > 0)

        if not prev:
            d_state, d_note = "none", ("Немає попередньої сесії для порівняння — "
                                       "Δ OI з'явиться після наступного оновлення даних.")
        elif changed == 0:
            d_state, d_note = "flat", (f"Open Interest не змінився з {s['prev_label']} — "
                                       "провайдер ще не виклав нову нічну обробку.")
        else:
            d_state, d_note = "ok", ("Зміна відкритого інтересу проти "
                                     f"{s['prev_label']}: {changed} страйків рухнулись. "
                                     "Зростання OI означає, що рівень став вагомішим.")

        d_cap = _cap([abs(v) for v in moves]) if changed else 10

        total_gex = sum(g["gex"] for g in s["gex"]) / 1e6 if s["gex"] else None
        # Стелі GEX — окремо вгору і вниз. Симетрична вісь марнувала
        # половину полотна, коли гамма майже вся з одного боку; мінімум
        # у чверть протилежного боку лишає нульову лінію видимою.
        # Тільки ті страйки, що реально потрапили на графік: s["gex"] покриває
        # весь ланцюг, а малюємо ми лише відібране вікно — інакше далекі
        # страйки задирали стелю і бари губились біля нуля.
        on_chart = set(strikes)
        gvis = [g for g in s["gex"] if g["strike"] in on_chart]
        gpos = [g["gex"] / 1e6 for g in gvis if g["gex"] > 0]
        gneg = [-g["gex"] / 1e6 for g in gvis if g["gex"] < 0]

        def _gcap(vals):
            if not vals:
                return 0.0
            v = sorted(vals)
            return v[min(len(v) - 1, int(len(v) * 0.97))] * 1.12

        up, dn = _gcap(gpos), _gcap(gneg)
        span   = max(up, dn, 0.01)
        gex_up = max(up, span * 0.25)
        gex_dn = max(dn, span * 0.25)

        pull, direction = "—", "flat"
        if spot and mp:
            gap = round(mp - spot)
            if gap > 0:   pull, direction = f"↑ {abs(gap)} пт вище", "up"
            elif gap < 0: pull, direction = f"↓ {abs(gap)} пт нижче", "down"
            else:         pull = "на рівні spot"

        tickvals, ticktext = _ticks(strikes)

        meta.append(dict(
            base=s["base"],
            label=s["expiry"].strftime("%d %b %Y") + " · " + (s["contract_type"] or "—"),
            spot_txt=_sp(spot), pain_txt=_sp(mp),
            pull_txt=pull, pull_dir=direction,
            pcr=pc_oi, vol_pc=pc_vol,
            gex_txt=(f"{total_gex:+.1f}M" if total_gex is not None else "—"),
            gex_dir=("up" if (total_gex or 0) >= 0 else "down"),
            stamp=s["stamp"],
            shapes=shapes, annotations=notes,
            view=s["view"], nstrikes=len(strikes),
            tickvals=tickvals, ticktext=ticktext,
            oi_call=s["oi_call"], oi_put=s["oi_put"],
            vol_call=s["vol_call"], vol_put=s["vol_put"],
            gex_up=round(gex_up, 4), gex_dn=round(gex_dn, 4), d_cap=d_cap,
            c_oi=c_oi, p_oi=p_oi, c_vol=c_vol, p_vol=p_vol,
            call_walls=[{"s": r["strike"], "oi": r["call_oi"]}
                        for r in sorted(rows, key=lambda r: r["call_oi"], reverse=True)[:3]],
            put_walls=[{"s": r["strike"], "oi": r["put_oi"]}
                       for r in sorted(rows, key=lambda r: r["put_oi"], reverse=True)[:3]],
            d_state=d_state, d_note=d_note,
            delta_call=d_call, delta_put=d_put,
        ))
    return meta


HTML = r"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DAX Options — Eurex</title>
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
  --ease:cubic-bezier(.4,0,.2,1);
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
.masthead .mark{color:var(--text);font-weight:600;letter-spacing:.2em}

.assets{display:flex;border:1px solid var(--rule-lit);border-radius:3px;overflow:hidden}
.assets button{font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.14em;
  padding:5px 18px;background:transparent;color:var(--muted);border:none;
  border-left:1px solid var(--rule-lit);cursor:pointer;
  transition:background .2s var(--ease),color .2s var(--ease)}
.assets button:first-child{border-left:none}
.assets button:hover:not(.on){background:#141417;color:var(--text)}
.assets button.on{background:var(--spot);color:#000}

.readout{display:grid;grid-template-columns:repeat(5,1fr);
  border-bottom:1px solid var(--rule)}
.metric{padding:15px 20px 14px;border-left:1px solid var(--rule)}
.metric:first-child{border-left:none}
.metric .val{font-family:var(--mono);font-size:26px;font-weight:400;
  letter-spacing:-.015em;line-height:1;color:var(--text);
  font-variant-numeric:tabular-nums;transition:color .3s var(--ease)}
.metric .key{font-family:var(--mono);font-size:9.5px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--faint);margin-top:9px}
.metric .sub{font-family:var(--mono);font-size:11px;color:var(--muted);
  margin-top:3px;font-variant-numeric:tabular-nums}
.metric.is-spot .val{color:var(--spot)}
.metric.is-pain .val{color:var(--pain)}
.pull{font-weight:500}
.pull.up{color:var(--open)} .pull.down{color:var(--close)}
.gexval.up{color:var(--spot)} .gexval.down{color:var(--close)}

.ladders{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid var(--rule)}
.ladder{padding:11px 20px 12px;border-left:1px solid var(--rule)}
.ladder:first-child{border-left:none}
.ladder h2{font-family:var(--mono);font-size:9.5px;font-weight:400;
  letter-spacing:.16em;text-transform:uppercase;color:var(--faint);margin-bottom:7px}
.rung{position:relative;display:flex;justify-content:space-between;align-items:center;
  padding:3px 7px;margin-bottom:2px;font-family:var(--mono);font-size:12px;
  overflow:hidden;font-variant-numeric:tabular-nums}
.rung .fill{position:absolute;inset:0 auto 0 0;z-index:0;
  transition:width .42s var(--ease)}
.rung.call .fill{background:rgba(110,110,118,.30)}
.rung.put .fill{background:rgba(255,255,255,.13)}
.rung .k,.rung .v{position:relative;z-index:1}
.rung .k{color:var(--text);letter-spacing:.04em}
.rung .v{color:var(--muted);font-size:11px}

.bar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:10px 20px;
  border-bottom:1px solid var(--rule)}
.seg{display:flex;border:1px solid var(--rule-lit);border-radius:3px;overflow:hidden}
.seg button{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;
  text-transform:uppercase;padding:6px 14px;background:transparent;color:var(--muted);
  border:none;border-left:1px solid var(--rule-lit);cursor:pointer;
  transition:background .2s var(--ease),color .2s var(--ease)}
.seg button:first-child{border-left:none}
.seg button:hover:not(:disabled):not(.on){background:#141417;color:var(--text)}
.seg button.on{background:var(--text);color:var(--void);font-weight:600}
.seg button:disabled{color:#2C2C31;cursor:not-allowed}
.seg.zoom button{padding:6px 11px;font-size:13px;line-height:1}

select{font-family:var(--mono);font-size:11px;color:var(--text);
  background:var(--raised);border:1px solid var(--rule-lit);border-radius:3px;
  padding:6px 10px;cursor:pointer;outline:none;max-width:250px;
  transition:border-color .2s var(--ease)}
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
.tape b.flat{color:var(--faint)}

.note{font-family:var(--mono);font-size:10.5px;color:var(--faint);
  padding:9px 20px 0;min-height:17px;letter-spacing:.03em;
  transition:color .3s var(--ease)}
.note.warn{color:var(--pain)}

.stage{padding:2px 6px 20px}
.hidden{display:none}
/* Крос-фейд замість ривка: графік гасне на час перемальовування
   і повертається, коли Plotly вже домалював новий стан. */
#chart,#heat-stage{transition:opacity .2s var(--ease)}
.js-plotly-plot .plotly .modebar{background:transparent!important;opacity:.28;
  transition:opacity .25s var(--ease)}
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
  .metric .val{font-size:21px}
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
  <span id="m-stamp">—</span>
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
  <div class="metric">
    <div class="val"><span class="gexval" id="m-gex">—</span></div>
    <div class="key">Net GEX</div>
    <div class="sub">EUR на 1 % руху</div>
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
    <button id="b-oi" class="on" onclick="setMode('oi')">OI</button>
    <button id="b-vol" onclick="setMode('vol')">Volume</button>
    <button id="b-gex" onclick="setMode('gex')">GEX</button>
    <button id="b-delta" onclick="setMode('delta')">&Delta; OI</button>
    <button id="b-heat" onclick="setMode('heat')">Heatmap</button>
  </div>
  <div class="seg zoom" role="group" aria-label="Масштаб">
    <button id="z-in" onclick="zoom(-1)" title="Щільніше">&minus;</button>
    <button id="z-out" onclick="zoom(1)" title="Ширше">+</button>
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
var NTRACES   = NTRACES_VALUE;
var VIEW      = VIEW_STRIKES_VALUE;
var T         = 7;

var curIdx   = 0;
var curMode  = 'oi';
var viewWide = VIEW;      // скільки страйків показуємо одразу
var gen      = 0;         // покоління рендера — захист від гонки

function num(n){
  if(n === null || n === undefined) return '—';
  return Math.round(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}
function signed(n){
  if(!n) return '0';
  return (n > 0 ? '+' : '−') + num(Math.abs(n));
}

/* Які трейси показати. Індекси рахуємо від m.base — він проставлений
   у Python, тож JS не мусить сам вгадувати зсув серії. */
function visibility(){
  var v = new Array(NTRACES).fill(false);
  var o = META[curIdx].base;
  if(curMode === 'oi')         { v[o]   = v[o+1] = true; }
  else if(curMode === 'vol')   { v[o+2] = v[o+3] = true; }
  else if(curMode === 'gex')   { v[o+4] = true; }
  else if(curMode === 'delta') { v[o+5] = v[o+6] = true; }
  return v;
}

function yRange(m){
  if(curMode === 'oi')    return [-m.oi_put,  m.oi_call];
  if(curMode === 'vol')   return [-m.vol_put, m.vol_call];
  if(curMode === 'gex')   return [-m.gex_dn, m.gex_up];
  return [-m.d_cap, m.d_cap];
}
function yTitle(){
  return curMode === 'vol'   ? 'VOLUME'
       : curMode === 'gex'   ? 'GEX  ·  M € / 1%'
       : curMode === 'delta' ? 'Δ OPEN INTEREST'
       : 'OPEN INTEREST';
}

/* Видиме вікно в індексах страйків, відцентроване на поточному вигляді. */
function xRange(m){
  var n = m.nstrikes;
  if(n <= viewWide) return [-0.5, n - 0.5];
  var mid = (m.view[0] + m.view[1]) / 2;
  var half = viewWide / 2;
  var lo = Math.max(-0.5, mid - half);
  var hi = Math.min(n - 0.5, lo + viewWide);
  lo = Math.max(-0.5, hi - viewWide);
  return [lo, hi];
}

var KEYS = {
  oi:    [['Calls','var(--ash)'], ['Puts','var(--chalk)']],
  vol:   [['Calls','var(--ash)'], ['Puts','var(--chalk)']],
  gex:   [['лонг гамма · гасить рух','var(--spot)'], ['шорт гамма · підсилює','var(--close)']],
  delta: [['позиції відкрито','var(--open)'], ['позиції закрито','var(--close)']],
  heat:  [['Calls','var(--ash)'], ['Puts','var(--chalk)']]
};

var NOTES = {
  oi:   'Puts вниз — підтримка. Calls вгору — опір. Вісь обрізана за перцентилем, '
      + 'щоб поодинокі величезні страйки не з\'їдали графік — повний розмах видно скролом.',
  vol:  'Обсяг торгів за сесію. На відміну від OI оновлюється протягом дня.',
  gex:  'Позитивний GEX — маркет-мейкери гасять рух і рівень тримає. '
      + 'Негативний — хеджування підсилює рух у той самий бік.',
  heat: 'Вертикальні смуги — страйки, що тримають OI одразу на кількох експіраціях. '
      + 'Це найстійкіші рівні.'
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
  document.getElementById('pick').innerHTML = META.map(function(m,i){
    return '<option value="' + i + '">' + m.label + '</option>';
  }).join('');
  document.getElementById('pick').value = String(curIdx);
}

function render(){
  var my = ++gen;                      // мітка цього виклику
  var m  = META[curIdx];
  var heat = (curMode === 'heat');

  document.getElementById('main-stage').classList.toggle('hidden', heat);
  document.getElementById('heat-stage').classList.toggle('hidden', !heat);

  if(heat){
    var hd = document.getElementById('heatmap');
    if(hd) Plotly.Plots.resize(hd);
  } else {
    var gd = document.getElementById('chart');
    gd.style.opacity = '0.35';         // гасимо на час перемальовування
    var lay = {
      shapes: m.shapes,
      annotations: m.annotations,
      'xaxis.range': xRange(m),
      'xaxis.tickvals': m.tickvals,
      'xaxis.ticktext': m.ticktext,
      'yaxis.range': yRange(m),
      'yaxis.autorange': false,
      'yaxis.title.text': yTitle()
    };
    /* Вісь одна (Volume переїхав у власну вкладку), тож зникла колишня
       пара xaxis/xaxis2 з matches, через яку діапазон відкочувався назад.
       Лишається гонка промісів: якщо під час restyle прилетів новий
       render, старий не має права домальовувати — звідси мітка `my`. */
    Plotly.restyle(gd, { visible: visibility() }).then(function(){
      if(my !== gen) return;
      return Plotly.relayout(gd, lay);
    }).then(function(){
      if(my !== gen) return;
      gd.style.opacity = '1';
    }).catch(function(){ gd.style.opacity = '1'; });
  }

  document.getElementById('m-stamp').textContent = m.stamp;
  document.getElementById('m-spot').textContent  = m.spot_txt;
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

  /* Δ OI у стрічці: прочерк, коли порівнювати нема з чим — інакше «0»
     читається як «нічого не змінилось», хоча дані просто відсутні. */
  var dc = document.getElementById('t-dc'), dp = document.getElementById('t-dp');
  dc.textContent = m.d_state === 'none' ? '—' : signed(m.delta_call);
  dp.textContent = m.d_state === 'none' ? '—' : signed(m.delta_put);
  dc.className = m.d_state === 'ok' ? '' : 'flat';
  dp.className = m.d_state === 'ok' ? '' : 'flat';

  drawLadder(m.call_walls, 'l-call', 'call');
  drawLadder(m.put_walls,  'l-put',  'put');
  drawKey();

  document.getElementById('b-delta').disabled = (m.d_state === 'none');

  var note = document.getElementById('note');
  if(curMode === 'delta'){
    note.textContent = m.d_note;
    note.className = 'note' + (m.d_state === 'ok' ? '' : ' warn');
  } else {
    note.textContent = NOTES[curMode] || '';
    note.className = 'note';
  }

  document.getElementById('z-in').disabled  = viewWide <= 14;
  document.getElementById('z-out').disabled = viewWide >= m.nstrikes;
}

function setSeries(i){
  var next = parseInt(i, 10);
  if(next === curIdx) return;
  curIdx = next;
  /* Δ OI недоступний на новій експірації — не лишаємо порожнє полотно */
  if(curMode === 'delta' && META[curIdx].d_state === 'none') curMode = 'oi', syncModeButtons();
  render();
}
function syncModeButtons(){
  [['oi','b-oi'],['vol','b-vol'],['gex','b-gex'],['delta','b-delta'],['heat','b-heat']]
    .forEach(function(p){
      document.getElementById(p[1]).classList.toggle('on', curMode === p[0]);
    });
}
function setMode(mode){
  if(mode === curMode) return;
  curMode = mode;
  syncModeButtons();
  render();
}
function zoom(dir){
  var step = Math.max(6, Math.round(viewWide * 0.35));
  viewWide = Math.max(14, Math.min(META[curIdx].nstrikes, viewWide + dir * step));
  render();
}

var booted = false;
function boot(){ fillPicker(); syncModeButtons(); render(); booted = true; }
var chartEl = document.getElementById('chart');
if(chartEl && chartEl.on){
  chartEl.on('plotly_afterplot', function(){ if(!booted) setTimeout(boot, 60); });
}
setTimeout(function(){ if(!booted) boot(); }, 500);
setTimeout(function(){ booted = false; boot(); }, 1600);
window.addEventListener('resize', function(){
  var el = document.getElementById(curMode === 'heat' ? 'heatmap' : 'chart');
  if(el) Plotly.Plots.resize(el);
});
</script>
</body>
</html>"""


def build_html(series):
    for i, s in enumerate(series):
        s["base"] = i * TRACES_PER_SERIES

    fig  = build_figure(series)
    meta = build_meta(series)

    cfg = {"scrollZoom": True, "displayModeBar": True, "displaylogo": False,
           "modeBarButtonsToRemove": ["select2d", "lasso2d", "toggleSpikelines"]}
    main_div = fig.to_html(full_html=False, include_plotlyjs=PLOTLY_JS,
                           div_id="chart", config=cfg)

    hm = build_heatmap(series)
    heat_div = (hm.to_html(full_html=False, include_plotlyjs=False, div_id="heatmap",
                           config={"scrollZoom": True, "displayModeBar": False})
                if hm else
                "<div id='heatmap' style='color:#45454C;padding:44px;text-align:center;"
                "font-family:monospace;font-size:12px'>Недостатньо даних для heatmap</div>")

    return (HTML
            .replace("PLOTLY_DIV",         main_div)
            .replace("HEATMAP_DIVS",       heat_div)
            .replace("NTRACES_VALUE",      str(len(series) * TRACES_PER_SERIES))
            .replace("VIEW_STRIKES_VALUE", str(VIEW_STRIKES))
            .replace("EXPIRY_META_JSON",   json.dumps(meta, ensure_ascii=False)))


def main():
    init_db()
    asset = "DAX"

    print("\n  Перевіряю базу даних…")
    trade_date, db_data = load_latest(asset)

    provider = EurexProvider(stats_id="70044", asset=asset)
    on_eurex = provider._latest_trade_date()
    print("  Eurex: %s   БД: %s" % (on_eurex or "НЕДОСТУПНИЙ", trade_date or "—"))

    if on_eurex is None:
        # Не плутати з «немає нової сесії»: там усе гаразд, а тут ми просто
        # не змогли спитати. Пишемо голосно, щоб у логах збірки було видно
        # справжню причину застряглої дати.
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
    print("  Spot: %s" % (f"{spot:,.1f}" if spot else "Yahoo недоступний — put-call parity"))

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

    prune_sessions(asset, keep=20)

    prev_date, prev_chains = load_previous(asset)
    prev_lookup = {c["expiry"]: c["strikes"] for c in prev_chains}
    prev_label  = prev_date.strftime("%d %b") if prev_date else "—"
    print("  Δ OI проти %s" % (prev_date or "— (потрібна друга сесія)"))

    _, db_data = load_latest(asset)
    stamp = trade_date.strftime("%d %b %Y").upper()

    series = []
    for d in sorted([x for x in db_data if x["strikes"]], key=lambda x: x["expiry"]):
        a       = d["analytics"] or {}
        und     = a.get("futures_px") or a.get("underlying")
        mp      = a.get("max_pain")
        center  = und or mp
        rows    = _window(d["strikes"], center)
        strikes = [r["strike"] for r in rows]

        series.append(dict(
            expiry=d["expiry"], contract_type=d["contract_type"],
            rows=rows, strikes=strikes,
            gex=d.get("gex_data") or [],
            spot=und, max_pain=mp, pcr=a.get("pcr"),
            center=center, view=_view_range(strikes, center),
            prev=prev_lookup.get(d["expiry"], {}),
            prev_label=prev_label,
            stamp=stamp,
            # Стелі — на кожну серію окремо: тижнева має OI у сотні,
            # місячна — у десятки тисяч; спільна шкала вбила б одну з них.
            oi_call  = _cap(r["call_oi"]         for r in rows),
            oi_put   = _cap(r["put_oi"]          for r in rows),
            vol_call = _cap((r["call_vol"] or 0) for r in rows),
            vol_put  = _cap((r["put_vol"]  or 0) for r in rows),
        ))

    if not series:
        print("  Немає даних для рендеру.")
        return

    total_strikes = sum(len(s["rows"]) for s in series)
    print("\n  Рендер: %d експірацій, %d страйків" % (len(series), total_strikes))

    html = build_html(series)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "dax_options.html"
    out.write_text(html, encoding="utf-8")
    print("  %s готовий%s\n" % (out.name, "" if HEADLESS else " — відкриваю браузер"))
    if not HEADLESS:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
