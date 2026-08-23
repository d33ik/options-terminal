"""
Титульна сторінка сайту — два посилання на дашборди.

Запускається після visualize.py та visualize_us.py, кладе index.html
поряд із ними. Локально не потрібна, але й не заважає.

  OPTIONS_OUT_DIR — той самий каталог, що й у дашбордів
"""

import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

OUT_DIR  = Path(os.environ.get("OPTIONS_OUT_DIR") or Path(__file__).parent)
BASE_DIR = Path(__file__).parent

# Час Варшави: UTC+1 узимку, UTC+2 влітку. Точний перехід тут не критичний —
# показуємо позначку разом із UTC, щоб не вводити в оману.
now_utc = datetime.now(timezone.utc)
warsaw  = now_utc + timedelta(hours=2)
STAMP   = warsaw.strftime("%d.%m.%Y  %H:%M")

MONTHS = ["січ", "лют", "бер", "кві", "тра", "чер",
          "лип", "сер", "вер", "жов", "лис", "гру"]


def data_date(db_name: str, asset: str) -> str:
    """
    За яку торгову сесію лежать дані в базі.

    Це головне, що треба бачити на титульній: якщо провайдер не оновився
    або збірка підтягнула стару сторінку — дата одразу це покаже, і не
    доведеться гадати, свіжі дані чи ні.
    """
    path = BASE_DIR / db_name
    if not path.exists():
        return "—"
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT trade_date FROM sessions WHERE asset=? "
                "ORDER BY trade_date DESC LIMIT 1", (asset,)
            ).fetchone()
        finally:
            con.close()
        if not row:
            return "—"
        d = datetime.strptime(row[0], "%Y-%m-%d").date()
        return f"{d.day} {MONTHS[d.month - 1]} {d.year}"
    except Exception:
        return "—"


def _short(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d").date()
    return f"{d.day} {MONTHS[d.month - 1]}"


def oi_status(db_name: str, asset: str) -> tuple[str, str]:
    """
    Чи вдалось підтягнути нову порцію Open Interest.

    Дата знімка сама по собі нічого не гарантує: OCC викладає нічний OI
    у нефіксований час, і знімок може містити ще позавчорашні цифри.
    Тому порівнюємо СУМУ OI двох останніх сесій у базі:

      сума змінилась  → провайдер віддав свіжий OI, Δ OI має сенс
      сума та сама    → OI ще не оновився, Δ OI покаже нулі
      одна сесія      → перша збірка, порівнювати нема з чим

    Повертає (клас_для_css, текст).
    """
    path = BASE_DIR / db_name
    if not path.exists():
        return "none", "бази немає"
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                """SELECT s.trade_date, SUM(st.call_oi + st.put_oi) AS total
                   FROM sessions s
                   JOIN chains  c  ON c.session_id = s.id
                   JOIN strikes st ON st.chain_id  = c.id
                   WHERE s.asset = ?
                   GROUP BY s.id
                   ORDER BY s.trade_date DESC
                   LIMIT 2""",
                (asset,),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return "none", "не вдалось прочитати"

    if not rows:
        return "none", "даних немає"
    if len(rows) < 2:
        return "none", "перша збірка — порівнювати ще нема з чим"

    (_, now_total), (prev_date, prev_total) = rows[0], rows[1]
    if now_total != prev_total:
        return "fresh", f"свіжий OI · порівняння з {_short(prev_date)}"
    return "stale", f"OI ще не оновився з {_short(prev_date)}"


CARDS = [
    dict(href="dax_options.html", code="DAX",
         title="DAX 40", venue="Eurex · settlement",
         slabel="Сесія",
         session=data_date("options_data.db", "DAX"),
         oi=oi_status("options_data.db", "DAX"),
         note="Опціони на німецький індекс. Eurex публікує сесію наступного "
              "дня близько полудня — дата вище і є тією сесією."),
    dict(href="us_options.html", code="SPX / NDX",
         title="S&P 500 · Nasdaq-100", venue="Cboe · delayed",
         # Для США в базі лежить дата завантаження, а не сесія: Open Interest
         # приходить за попереднє закриття, а обсяги — поточні. Називати це
         # «сесією» було б неточно.
         slabel="Знімок",
         session=data_date("us_options.db", "SPX"),
         oi=oi_status("us_options.db", "SPX"),
         note="Індексні опціони США. Open Interest — за попереднє закриття, "
              "обсяги й ціна — з затримкою 15 хв. Точний час знімка вказано "
              "в шапці самого дашборда."),
]

HTML = """<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Options Terminal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --void:#000; --rule:#1A1A1D; --rule-lit:#26262B;
  --text:#E8E8EA; --muted:#6E6E76; --faint:#45454C; --spot:#22D3EE;
  --mono:'IBM Plex Mono',ui-monospace,'SF Mono',Consolas,monospace;
  --sans:'IBM Plex Sans',-apple-system,'Segoe UI',system-ui,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--void);color:var(--text);font-family:var(--sans);
     min-height:100vh;display:flex;flex-direction:column}
:focus-visible{outline:1px solid var(--spot);outline-offset:2px}

header{display:flex;align-items:center;gap:14px;padding:11px 24px;
  border-bottom:1px solid var(--rule);font-family:var(--mono);font-size:10px;
  letter-spacing:.14em;text-transform:uppercase;color:var(--faint)}
header .dot{width:5px;height:5px;border-radius:50%;background:var(--spot);
  box-shadow:0 0 6px var(--spot)}
header .mark{color:var(--text);font-weight:600;letter-spacing:.2em}
header .grow{flex:1}

main{flex:1;display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--rule)}
a.card{display:flex;flex-direction:column;justify-content:center;gap:14px;
  background:var(--void);padding:64px 44px;text-decoration:none;color:inherit;
  transition:background .18s}
a.card:hover{background:#0B0B0D}
.code{font-family:var(--mono);font-size:38px;font-weight:400;letter-spacing:-.02em;
  color:var(--spot);line-height:1}
.title{font-size:17px;font-weight:500}
.venue{font-family:var(--mono);font-size:10px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--faint)}
.session{display:flex;align-items:baseline;gap:10px;
  padding:9px 0 10px;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.slabel{font-family:var(--mono);font-size:9.5px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--faint)}
.sval{font-family:var(--mono);font-size:15px;color:var(--text)}
.oi{display:flex;align-items:center;gap:8px;font-family:var(--mono);
  font-size:11px;color:var(--muted);margin-top:-2px}
.oi-dot{width:7px;height:7px;border-radius:50%;flex:none;background:#3A3A42}
.oi.fresh .oi-dot{background:#2DD4A7;box-shadow:0 0 6px #2DD4A7}
.oi.fresh{color:#2DD4A7}
.oi.stale .oi-dot{background:#E8A317}
.oi.stale{color:#E8A317}
.note{font-size:13px;color:var(--muted);max-width:38ch;line-height:1.5}
.go{font-family:var(--mono);font-size:11px;letter-spacing:.1em;color:var(--faint);
  text-transform:uppercase;margin-top:6px}
a.card:hover .go{color:var(--spot)}

footer{padding:13px 24px;border-top:1px solid var(--rule);font-family:var(--mono);
  font-size:10px;color:var(--faint);letter-spacing:.08em;
  display:flex;gap:18px;flex-wrap:wrap}

@media (max-width:760px){
  main{grid-template-columns:1fr}
  a.card{padding:40px 26px}
  .code{font-size:30px}
}
</style>
</head>
<body>

<header>
  <span class="dot"></span>
  <span class="mark">Options Terminal</span>
  <span class="grow"></span>
  <span>оновлено __STAMP__</span>
</header>

<main>
__CARDS__
</main>

<footer>
  <span>Збірки: 08:00, 11:00, 11:40, 15:00, 23:30 за Варшавою</span>
  <span>·</span>
  <span>Джерела: Eurex, Cboe</span>
</footer>

</body>
</html>"""

card_tpl = """  <a class="card" href="{href}">
    <div>
      <div class="code">{code}</div>
      <div class="venue" style="margin-top:10px">{venue}</div>
    </div>
    <div class="title">{title}</div>
    <div class="session">
      <span class="slabel">{slabel}</span>
      <span class="sval">{session}</span>
    </div>
    <div class="oi {oi_cls}">
      <span class="oi-dot"></span>
      <span>{oi_txt}</span>
    </div>
    <div class="note">{note}</div>
    <div class="go">Відкрити &rarr;</div>
  </a>"""


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cards = "\n".join(
        card_tpl.format(**{**c, "oi_cls": c["oi"][0], "oi_txt": c["oi"][1]})
        for c in CARDS
    )
    html = HTML.replace("__CARDS__", cards).replace("__STAMP__", STAMP)
    out = OUT_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"  {out.name} готовий")


if __name__ == "__main__":
    main()
