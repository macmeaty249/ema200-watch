# EMA 200 Watch

This checks your stocks after every market close and pushes an alert to your phone when one crosses its 200-day EMA. It also keeps a chart page of every stock against its EMA. It runs free on GitHub Actions, and nothing needs to stay on your computer.

## The rule

| Signal | What happened |
|---|---|
| **BUY** | The stock **closes above** its 200 EMA (it was below before) |
| **SELL** | The stock **closes below** its 200 EMA (it was above before) |

Built-in best practices:
- **It uses daily closes only.** Intraday dips through the line don't count.
- **It waits for confirmation.** A signal needs **2 closes in a row** on the new side (`confirm_days`). This filters out most fake-outs, where the price pokes through for a day and snaps back.
- **You can add a buffer.** Set `buffer_pct` to, say, `1.0` and the close must clear the line by 1%. Use this if a stock keeps chopping around the line.
- **It shows the EMA slope.** Each alert says whether the EMA itself is rising or falling. A buy while the EMA is still falling is a weaker signal than one while it's rising.
- **It gives one alert per cross.** The script remembers each stock's side in `state.json`, so you won't get repeat pings.

Know the limits: the 200 EMA is a slow trend filter. It gets you out of long declines and back into long uptrends, but you'll miss the first part of every move. In sideways markets it will flip back and forth, and the confirmation and buffer settings help with that. This tool is for information only and isn't financial advice.

## Setup (about 10 minutes, once)

1. **Get the phone app.** Install **ntfy** from the App Store or Play Store. Tap **+** and subscribe to a hard-to-guess topic name, like `matt-ema-7f3k92`. Anyone who knows the name can see your alerts, so make it random.
2. **Create the repo.** On github.com, go to **New repository**, name it `ema200-watch` and make it **Public**. Public is needed for the free chart page.
3. **Upload the files.** Choose **Add file → Upload files** and drag in everything from this folder, **including the `.github` folder**. If the `.github` folder won't upload, use **Add file → Create new file**, name it `.github/workflows/ema-watch.yml` and paste in that file's contents.
4. **Add your topic as a secret.** Go to **Settings → Secrets and variables → Actions → New repository secret**. Name it `NTFY_TOPIC`, set the value to your topic name and save.
5. **Turn on the chart page.** Go to **Settings → Pages**. Under Source, pick **Deploy from a branch**, then choose branch **main** and folder **/docs**. Your page will be at `https://<your-username>.github.io/ema200-watch/`, and tapping an alert opens it.
6. **Run it once.** Go to **Actions → EMA 200 check → Run workflow**. The first run records where each stock stands and doesn't send alerts. After that it runs every weekday around 4:35pm ET.

## Backtest

Each run also refreshes `backtest.html`, which is linked from the chart page. It shows what $10,000 split across your watchlist would be worth today if you'd started 5 years, 3 years, 1 year or 1 month ago. It compares buy & hold, the EMA 1-day rule and the EMA 2-day rule. The amount and the split are under `"backtest"` in `config.json`. A new ticker joins the backtest once you give it a weight there.

## Day to day

- **Add or remove stocks** by editing `tickers.txt` on GitHub (the GitHub mobile app works). Put one ticker per line. Saving the file triggers a fresh check right away. New stocks don't alert until their first real cross after you add them.
- **Change settings** in `config.json`:
  - `confirm_days`: closes in a row needed (default `2`; use `1` for the raw rule)
  - `buffer_pct`: how far past the line the close must be (default `0`)
  - `near_line_pct`: flags stocks within this % of the line on the chart page (default `2`)
  - `daily_summary`: set it to `true` for a low-priority push every evening with every stock's status
- If a ticker fails to load (a typo, or the stock was delisted), you get one push saying which one.
