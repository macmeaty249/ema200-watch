"""Backtest: what would $X spread across your watchlist have earned?

For each lookback period (5y, 3y, 1y, 1m) it compares three ways of holding
each stock's slice of the portfolio:
  - Buy & hold:   buy on the start date, never sell
  - EMA 1-day:    be in the stock only while it's confirmed ABOVE its 200 EMA (1 close to flip)
  - EMA 2-day:    same, but 2 closes in a row are needed to flip

Rules (no hindsight):
  - Weights are fixed in config.json and decided up front, not from past returns.
  - On the start date, an EMA slice buys only if the stock is already ABOVE its EMA
    (computed from prices before that date); otherwise it waits in cash for a buy signal.
  - Trades happen at the closing price on the signal day. No commissions; cash earns 0%.
  - Prices are dividend-adjusted, so dividends count as if reinvested.
Writes docs/backtest.html and prints a summary.
"""

from __future__ import annotations

import html
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import ema_watch as w

PAGE = w.ROOT / "docs" / "backtest.html"
PERIODS = [("5 years", pd.DateOffset(years=5)), ("3 years", pd.DateOffset(years=3)),
           ("1 year", pd.DateOffset(years=1)), ("1 month", pd.DateOffset(months=1))]
STRATS = [("hold", "Buy & hold"), ("ema1", "EMA 1-day rule"), ("ema2", "EMA 2-day rule")]


def simulate(close: pd.Series, sides: pd.Series | None, start: int, amount: float) -> tuple[float, int]:
    """Return (ending value, number of trades) for one slice from bar `start` to the end."""
    if sides is None:  # buy & hold
        return amount * float(close.iloc[-1]) / float(close.iloc[start]), 1
    cash, shares, trades = amount, 0.0, 0
    if sides.iloc[start] == w.ABOVE:
        shares, cash, trades = cash / float(close.iloc[start]), 0.0, 1
    for i in range(start + 1, len(close)):
        s, p = sides.iloc[i], float(close.iloc[i])
        if s == sides.iloc[i - 1]:
            continue
        if s == w.ABOVE and shares == 0:
            shares, cash, trades = cash / p, 0.0, trades + 1
        elif s == w.BELOW and shares > 0:
            cash, shares, trades = shares * p, 0.0, trades + 1
    return cash + shares * float(close.iloc[-1]), trades


def run(fetch=w.fetch_closes) -> int:
    cfg = w.load_config()
    bt = cfg.get("backtest", {})
    amount = float(bt.get("amount", 10000))
    weights = {k.upper(): float(v) for k, v in bt.get("weights", {}).items()}
    tickers = [t for t in w.load_tickers() if t in weights] or w.load_tickers()
    if not weights:
        weights = {t: 1.0 for t in tickers}
    total_w = sum(weights[t] for t in tickers)
    alloc = {t: amount * weights[t] / total_w for t in tickers}
    skipped = [t for t in w.load_tickers() if t not in weights]

    data, errors = {}, {}
    for t in tickers:
        try:
            close = fetch(t, bt.get("history", "10y"))
            ema = close.ewm(span=cfg["ema_length"], adjust=False).mean()
            data[t] = {
                "close": close,
                "ema": ema,
                "ema1": w.confirmed_sides(close, ema, 1, cfg["buffer_pct"]),
                "ema2": w.confirmed_sides(close, ema, 2, cfg["buffer_pct"]),
            }
        except Exception as e:
            errors[t] = str(e)
    if not data:
        print("No data loaded:", errors, file=sys.stderr)
        return 1

    end = max(d["close"].index[-1] for d in data.values())
    results = []
    for label, offset in PERIODS:
        start_date = end - offset
        per = {"label": label, "start": None, "rows": [], "totals": {k: 0.0 for k, _ in STRATS}, "notes": []}
        for t in tickers:
            a = alloc[t]
            d = data.get(t)
            if d is None:
                per["notes"].append(f"{t}: no data ({errors.get(t)}), held as cash")
                for k, _ in STRATS:
                    per["totals"][k] += a
                continue
            close = d["close"]
            idx = close.index.searchsorted(start_date)
            if idx >= len(close) - 1 or close.index[0] > start_date + pd.Timedelta(days=7):
                per["notes"].append(f"{t}: not trading yet on start date, held as cash")
                row = {"t": t, "alloc": a, **{k: (a, 0) for k, _ in STRATS}}
            else:
                per["start"] = per["start"] or close.index[idx]
                if idx < cfg["ema_length"]:
                    per["notes"].append(f"{t}: only {idx} days of history before start, so its EMA was still warming up")
                row = {"t": t, "alloc": a,
                       "hold": simulate(close, None, idx, a),
                       "ema1": simulate(close, d["ema1"], idx, a),
                       "ema2": simulate(close, d["ema2"], idx, a)}
            for k, _ in STRATS:
                per["totals"][k] += row[k][0]
            per["rows"].append(row)
        per["start"] = per["start"] or start_date
        results.append(per)

    for per in results:
        print(f"\n{per['label']} (from {per['start']:%Y-%m-%d}):")
        for k, name in STRATS:
            v = per["totals"][k]
            print(f"  {name:16s} ${v:>10,.0f}  ({(v / amount - 1) * 100:+.1f}%)")

    PAGE.parent.mkdir(exist_ok=True)
    PAGE.write_text(render(results, amount, alloc, skipped, errors, end))
    return 0


def pct(v, base):
    return (v / base - 1) * 100


def cls(x):
    return "up" if x > 0.05 else "down" if x < -0.05 else ""


def render(results, amount, alloc, skipped, errors, end) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    summary = []
    for per in results:
        best = max(per["totals"], key=per["totals"].get)
        lines = []
        for k, name in STRATS:
            v = per["totals"][k]
            g = pct(v, amount)
            star = ' <span class="star">best</span>' if k == best else ""
            lines.append(f'<div class="line"><span>{name}{star}</span><span class="num"><b>${v:,.0f}</b> '
                         f'<span class="{cls(g)}">{v - amount:+,.0f} ({g:+.1f}%)</span></span></div>')
        summary.append(f'<section class="card"><div class="ph"><b>{per["label"]} ago</b>'
                       f'<span class="muted small">since {per["start"]:%b %d, %Y}</span></div>{"".join(lines)}</section>')

    details = []
    for per in results:
        rows = []
        for r in per["rows"]:
            tds = []
            for k, _ in STRATS:
                v, n = r[k]
                g = pct(v, r["alloc"])
                trades = f'<br><span class="muted">{n} trade{"s" if n != 1 else ""}</span>' if k != "hold" else ""
                tds.append(f'<td class="num"><span class="{cls(g)}">{g:+.0f}%</span><br>${v:,.0f}{trades}</td>')
            rows.append(f'<tr><td><b>{r["t"]}</b><br><span class="muted">${r["alloc"]:,.0f}</span></td>{"".join(tds)}</tr>')
        notes = "".join(f"<li>{html.escape(n)}</li>" for n in per["notes"])
        notes = f'<ul class="muted small">{notes}</ul>' if notes else ""
        details.append(
            f'<details><summary>{per["label"]}: stock by stock</summary><div class="tbl"><table>'
            f'<thead><tr><th>Stock</th>{"".join(f"<th class=num>{n}</th>" for _, n in STRATS)}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>{notes}</details>')

    weights = " · ".join(f"{t} ${a:,.0f}" for t, a in alloc.items())
    extra = ""
    if skipped:
        extra += f'<p class="muted small">Not in the backtest (no weight set in config.json): {", ".join(skipped)}</p>'
    if errors:
        extra += f'<p class="down small">Could not load: {", ".join(f"{t} ({e})" for t, e in errors.items())}</p>'
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EMA 200 Backtest</title>
<style>
:root{{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--line:#e7e5e4;--card:#fff;--up:#15803d;--down:#b91c1c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0c0a09;--fg:#f5f5f4;--muted:#a8a29e;--line:#292524;--card:#1c1917;--up:#4ade80;--down:#f87171}}}}
*{{box-sizing:border-box}}body{{margin:0;font:15px/1.45 -apple-system,system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:820px;margin:auto;padding:18px 14px 40px}}h1{{font-size:22px;margin:0 0 4px}}.muted{{color:var(--muted)}}.small{{font-size:13px}}
.tbl{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px;margin:12px 0}}
table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{font-size:12px;color:var(--muted);font-weight:600}}tr:last-child td{{border:0}}.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.up{{color:var(--up)}}.down{{color:var(--down)}}.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:12px 0}}.ph{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}}.line{{display:flex;justify-content:space-between;gap:8px;padding:5px 0;border-top:1px solid var(--line)}}.star{{font-size:11px;font-weight:700;padding:1px 6px;border-radius:99px;background:var(--up);color:var(--card);margin-left:4px}}details{{margin:10px 0}}summary{{cursor:pointer;font-weight:600;padding:6px 0}}a{{color:inherit}}
</style></head><body><main>
<h1>EMA 200 Backtest</h1>
<div class="muted">${amount:,.0f} split across your watchlist. Prices through {end:%b %d, %Y}. Updated {now}. <a href="./">Back to watchlist</a></div>
{"".join(summary)}
{"".join(details)}
<p class="small muted"><b>Split:</b> {weights}</p>{extra}
<p class="small muted"><b>How it's calculated:</b> Buy &amp; hold buys every slice on the start date and never sells.
The EMA rules buy a slice on the start date only if that stock was already above its 200 EMA (using prices up to then),
then buy and sell at the closing price whenever the rule flips. Cash earns nothing between trades, and there are no fees or taxes.
Prices are dividend-adjusted. Past results don't predict future returns. This is not financial advice.</p>
</main></body></html>"""


if __name__ == "__main__":
    sys.exit(run())
