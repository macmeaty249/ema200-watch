"""EMA 200 watcher.

Runs once per trading day after the close (via GitHub Actions):
  1. Downloads daily prices for every ticker in tickers.txt
  2. Computes the 200-day EMA
  3. Decides each stock's confirmed side of the line (ABOVE = buy/hold, BELOW = sell/stay out)
  4. Sends a phone push (ntfy.sh) when a stock's confirmed side flips
  5. Writes state.json (so each cross alerts once) and docs/index.html (the chart page)
"""

from __future__ import annotations

import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
TICKERS_FILE = ROOT / "tickers.txt"
STATE_FILE = ROOT / "state.json"
PAGE_FILE = ROOT / "docs" / "index.html"

ABOVE, BELOW, NONE = 1, -1, 0
LABEL = {ABOVE: "ABOVE", BELOW: "BELOW", NONE: "UNCONFIRMED"}


# ---------------------------------------------------------------- config / io
def load_config() -> dict:
    cfg = {
        "ema_length": 200,
        "confirm_days": 2,
        "buffer_pct": 0.0,
        "near_line_pct": 2.0,
        "chart_days": 365,
        "history": "5y",
        "daily_summary": False,
    }
    if CONFIG_FILE.exists():
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    return cfg


def load_tickers() -> list[str]:
    out = []
    for line in TICKERS_FILE.read_text().splitlines():
        t = line.split("#", 1)[0].strip().upper()
        if t and t not in out:
            out.append(t)
    return out


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def fetch_closes(ticker: str, period: str) -> pd.Series:
    import yfinance as yf  # imported here so the logic can be tested without it

    df = yf.download(ticker, period=period, interval="1d", auto_adjust=True, progress=False)
    if df is None or df.empty:
        raise ValueError("no price data returned")
    close = df["Close"]
    if isinstance(close, pd.DataFrame):  # newer yfinance returns multi-index columns
        close = close.iloc[:, 0]
    return close.dropna()


# ---------------------------------------------------------------- core logic
def confirmed_sides(close: pd.Series, ema: pd.Series, confirm_days: int, buffer_pct: float) -> pd.Series:
    """Side of the EMA a stock is on, only switching after `confirm_days`
    consecutive closes beyond the line (+/- buffer). Filters out whipsaws."""
    b = buffer_pct / 100.0
    raw = np.where(close > ema * (1 + b), ABOVE, np.where(close < ema * (1 - b), BELOW, NONE))
    state, run_side, run_len, out = NONE, NONE, 0, []
    for s in raw:
        if s != NONE and s == run_side:
            run_len += 1
        elif s != NONE:
            run_side, run_len = s, 1
        else:  # inside the buffer zone: no progress toward a flip
            run_side, run_len = NONE, 0
        if run_side != NONE and run_len >= confirm_days and run_side != state:
            state = run_side
        out.append(state)
    return pd.Series(out, index=close.index)


def analyze(ticker: str, close: pd.Series, cfg: dict) -> dict:
    n = cfg["ema_length"]
    ema = close.ewm(span=n, adjust=False).mean()
    sides = confirmed_sides(close, ema, cfg["confirm_days"], cfg["buffer_pct"])
    last_close, last_ema = float(close.iloc[-1]), float(ema.iloc[-1])
    lookback = min(20, len(ema) - 1)
    slope = (last_ema / float(ema.iloc[-1 - lookback]) - 1) * 100 if lookback > 0 else 0.0
    flips = sides[(sides != sides.shift()) & (sides != NONE)].iloc[1:] if len(sides) > 1 else sides.iloc[0:0]
    return {
        "ticker": ticker,
        "close": close,
        "ema": ema,
        "sides": sides,
        "flips": flips,
        "date": close.index[-1].strftime("%Y-%m-%d"),
        "last_close": last_close,
        "last_ema": last_ema,
        "dist_pct": (last_close / last_ema - 1) * 100,
        "ema_slope_pct": slope,
        "side": int(sides.iloc[-1]),
        "warmed_up": len(close) >= 2 * n,
    }


def money(x: float) -> str:
    return f"${x:,.2f}"


def signal_message(r: dict) -> tuple[str, str, str]:
    t, d = r["ticker"], r["dist_pct"]
    trend = "rising" if r["ema_slope_pct"] > 0 else "falling"
    if r["side"] == ABOVE:
        title = f"BUY signal: {t} crossed ABOVE its 200 EMA"
        tags = "chart_with_upwards_trend"
    else:
        title = f"SELL signal: {t} crossed BELOW its 200 EMA"
        tags = "chart_with_downwards_trend"
    body = (
        f"{t} closed at {money(r['last_close'])} on {r['date']}, "
        f"{abs(d):.1f}% {'above' if d >= 0 else 'below'} its 200 EMA ({money(r['last_ema'])}). "
        f"The EMA itself is {trend} ({r['ema_slope_pct']:+.1f}% over 20 days)."
    )
    return title, body, tags


def notify(title: str, body: str, tags: str = "", priority: str = "high") -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print(f"[no NTFY_TOPIC set] {title}\n  {body}")
        return
    import requests

    page_url = os.environ.get("PAGE_URL", "").strip()
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if page_url:
        headers["Click"] = page_url
    resp = requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, timeout=20)
    resp.raise_for_status()


# ---------------------------------------------------------------- chart page
def svg_chart(r: dict, days: int) -> str:
    close, ema, flips = r["close"].iloc[-days:], r["ema"].iloc[-days:], r["flips"]
    flips = flips[flips.index >= close.index[0]]
    W, H, pl, pr, pt, pb = 520, 220, 6, 50, 10, 24
    lo = float(min(close.min(), ema.min()))
    hi = float(max(close.max(), ema.max()))
    pad = (hi - lo) * 0.06 or 1
    lo, hi = lo - pad, hi + pad
    n = len(close)

    def x(i):
        return pl + (W - pl - pr) * (i / max(n - 1, 1))

    def y(v):
        return pt + (H - pt - pb) * (1 - (v - lo) / (hi - lo))

    def path(s):
        return " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(float(v)):.1f}" for i, v in enumerate(s.values))

    pos = {d: i for i, d in enumerate(close.index)}
    marks = []
    for d, s in flips.items():
        i = pos[d]
        cx, cy = x(i), y(float(close.loc[d]))
        if s == ABOVE:
            marks.append(f'<polygon class="buy" points="{cx:.1f},{cy-11:.1f} {cx-6:.1f},{cy-1:.1f} {cx+6:.1f},{cy-1:.1f}" transform="translate(0,-3)"><title>Buy {d:%b %d, %Y}</title></polygon>')
        else:
            marks.append(f'<polygon class="sell" points="{cx:.1f},{cy+11:.1f} {cx-6:.1f},{cy+1:.1f} {cx+6:.1f},{cy+1:.1f}" transform="translate(0,3)"><title>Sell {d:%b %d, %Y}</title></polygon>')

    grid = []
    for k in range(4):
        v = lo + (hi - lo) * (k + 0.5) / 4
        grid.append(f'<line class="grid" x1="{pl}" x2="{W-pr}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                    f'<text class="axis" x="{W-pr+6}" y="{y(v)+4:.1f}">{v:,.0f}</text>')
    first, last = close.index[0], close.index[-1]
    labels = (f'<text class="axis" x="{pl}" y="{H-4}">{first:%b %Y}</text>'
              f'<text class="axis" x="{W-pr}" y="{H-4}" text-anchor="end">{last:%b %d, %Y}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(r["ticker"])} price vs 200 EMA">'
            f'{"".join(grid)}<path class="ema" d="{path(ema)}"/><path class="price" d="{path(close)}"/>'
            f'{"".join(marks)}{labels}</svg>')


def render_page(results: list[dict], errors: dict, cfg: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    order = sorted(results, key=lambda r: abs(r["dist_pct"]))
    rows, cards = [], []
    for r in order:
        side = LABEL[r["side"]]
        cls = {ABOVE: "up", BELOW: "down"}.get(r["side"], "flat")
        near = abs(r["dist_pct"]) <= cfg["near_line_pct"]
        last_flip = f'{r["flips"].index[-1]:%b %d, %Y}' if len(r["flips"]) else "—"
        rows.append(
            f'<tr><td><a href="#{r["ticker"]}">{r["ticker"]}</a></td>'
            f'<td><span class="pill {cls}">{side}</span>{" <span class=near>near line</span>" if near else ""}</td>'
            f'<td class="num">{money(r["last_close"])}</td><td class="num">{money(r["last_ema"])}</td>'
            f'<td class="num {cls}">{r["dist_pct"]:+.1f}%</td><td class="num">{r["ema_slope_pct"]:+.1f}%</td>'
            f'<td>{last_flip}</td></tr>'
        )
        warn = "" if r["warmed_up"] else '<p class="warn">Less than 400 days of history — EMA may be less reliable.</p>'
        cards.append(
            f'<section class="card" id="{r["ticker"]}"><div class="card-head"><h2>{r["ticker"]}</h2>'
            f'<span class="pill {cls}">{side}</span><span class="muted">{money(r["last_close"])} · '
            f'{r["dist_pct"]:+.1f}% vs EMA</span></div>{svg_chart(r, cfg["chart_days"])}{warn}</section>'
        )
    err_html = "".join(f"<li><b>{html.escape(t)}</b>: {html.escape(e)}</li>" for t, e in errors.items())
    err_html = f'<div class="errors"><b>Could not load:</b><ul>{err_html}</ul></div>' if err_html else ""
    rule = (f'Signals need {cfg["confirm_days"]} consecutive daily close(s) on the new side'
            + (f' and a {cfg["buffer_pct"]}% buffer' if cfg["buffer_pct"] else "") + ".")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EMA 200 Watch</title>
<style>
:root{{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--line:#e7e5e4;--card:#fff;--up:#15803d;--down:#b91c1c;--ema:#d97706;--price:#1d4ed8}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0c0a09;--fg:#f5f5f4;--muted:#a8a29e;--line:#292524;--card:#1c1917;--up:#4ade80;--down:#f87171;--ema:#fbbf24;--price:#60a5fa}}}}
*{{box-sizing:border-box}}body{{margin:0;font:15px/1.45 -apple-system,system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:820px;margin:auto;padding:18px 14px 40px}}h1{{font-size:22px;margin:0 0 4px}}.muted{{color:var(--muted)}}
.tbl{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px;margin:16px 0}}
table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}
th{{font-size:12px;color:var(--muted);font-weight:600}}tr:last-child td{{border:0}}.num{{text-align:right;font-variant-numeric:tabular-nums}}
a{{color:inherit}}.pill{{font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px;border:1px solid currentColor}}
.up{{color:var(--up)}}.down{{color:var(--down)}}.flat{{color:var(--muted)}}.near{{font-size:11px;color:var(--ema);margin-left:6px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:14px 0}}
.card-head{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}.card h2{{margin:0;font-size:18px}}
svg{{width:100%;height:auto;margin-top:8px}}.price{{fill:none;stroke:var(--price);stroke-width:1.6}}.ema{{fill:none;stroke:var(--ema);stroke-width:2;stroke-dasharray:5 3}}
.grid{{stroke:var(--line)}}.axis{{fill:var(--muted);font-size:13px}}.buy{{fill:var(--up)}}.sell{{fill:var(--down)}}
.legend span{{margin-right:14px;font-size:13px}}.sw{{display:inline-block;width:16px;height:3px;vertical-align:middle;margin-right:5px}}
.warn,.errors{{color:var(--down);font-size:13px}}
</style></head><body><main>
<h1>EMA 200 Watch</h1>
<div class="muted">Updated {now}. {rule} Sorted by distance to the line. <a href="backtest.html">See the backtest</a></div>
<div class="legend" style="margin-top:8px"><span><i class="sw" style="background:var(--price)"></i>Close</span>
<span><i class="sw" style="background:var(--ema)"></i>200 EMA</span><span class="up">▲ buy</span><span class="down">▼ sell</span></div>
{err_html}
<div class="tbl"><table><thead><tr><th>Ticker</th><th>Side</th><th class="num">Close</th><th class="num">200 EMA</th>
<th class="num">vs EMA</th><th class="num">EMA 20d</th><th>Last signal</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>
{"".join(cards)}
<p class="muted" style="font-size:12px">For information only, not financial advice.</p>
</main></body></html>"""


# ---------------------------------------------------------------- main
def run(fetch=fetch_closes) -> int:
    cfg = load_config()
    tickers = load_tickers()
    state = load_state()
    results, errors, alerts = [], {}, []

    for t in tickers:
        try:
            r = analyze(t, fetch(t, cfg["history"]), cfg)
        except Exception as e:  # keep going if one ticker fails
            errors[t] = str(e)
            print(f"{t}: ERROR {e}", file=sys.stderr)
            continue
        results.append(r)
        prev = state.get(t, {}).get("side")
        if r["side"] != NONE and prev is not None and prev != NONE and r["side"] != prev:
            alerts.append(r)
        elif r["side"] != NONE and prev is None:
            print(f"{t}: now tracking, currently {LABEL[r['side']]} (no alert on first run)")
        state[t] = {"side": r["side"], "as_of": r["date"], "close": round(r["last_close"], 4), "ema": round(r["last_ema"], 4)}

    for t in list(state):  # forget tickers removed from tickers.txt
        if t not in tickers:
            del state[t]

    for r in alerts:
        title, body, tags = signal_message(r)
        notify(title, body, tags)

    if cfg["daily_summary"] and results:
        lines = [f"{r['ticker']}: {LABEL[r['side']]} ({r['dist_pct']:+.1f}%)" for r in sorted(results, key=lambda r: abs(r['dist_pct']))]
        notify("EMA 200 daily summary", "\n".join(lines), "bar_chart", priority="low")
    if errors:
        notify("EMA 200 watcher: data problem", "; ".join(f"{t}: {e}" for t, e in errors.items()), "warning", priority="default")

    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    PAGE_FILE.parent.mkdir(exist_ok=True)
    PAGE_FILE.write_text(render_page(results, errors, cfg))
    print(f"Checked {len(results)} ticker(s), {len(alerts)} signal(s), {len(errors)} error(s).")
    return 0


if __name__ == "__main__":
    sys.exit(run())
