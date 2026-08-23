"""
Титульна сторінка сайту — два посилання на дашборди.

Запускається після visualize.py та visualize_us.py, кладе index.html
поряд із ними. Локально не потрібна, але й не заважає.

  OPTIONS_OUT_DIR — той самий каталог, що й у дашбордів
"""

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

OUT_DIR = Path(os.environ.get("OPTIONS_OUT_DIR") or Path(__file__).parent)

# Час Варшави: UTC+1 узимку, UTC+2 влітку. Точний перехід тут не критичний —
# показуємо позначку разом із UTC, щоб не вводити в оману.
now_utc = datetime.now(timezone.utc)
warsaw  = now_utc + timedelta(hours=2)
STAMP   = warsaw.strftime("%d.%m.%Y  %H:%M")

CARDS = [
    dict(href="dax_options.html", code="DAX",
         title="DAX 40", venue="Eurex · settlement",
         note="Опціони на німецький індекс. Дані EOD, публікуються наступного дня."),
    dict(href="us_options.html", code="SPX / NDX",
         title="S&P 500 · Nasdaq-100", venue="Cboe · delayed",
         note="Індексні опціони США. Open Interest — за вчорашнім закриттям, обсяги — з затримкою 15 хв."),
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
  <span>Оновлюється автоматично двічі на день</span>
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
    <div class="note">{note}</div>
    <div class="go">Відкрити &rarr;</div>
  </a>"""


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cards = "\n".join(card_tpl.format(**c) for c in CARDS)
    html = HTML.replace("__CARDS__", cards).replace("__STAMP__", STAMP)
    out = OUT_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"  {out.name} готовий")


if __name__ == "__main__":
    main()
