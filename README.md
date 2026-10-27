# Ripple Radar

A daily US stock radar. Every weekday after the close it:

- sorts stocks into three price tiers: **Low $1–10**, **Medium $10–50**, **High $50+**
- scores each one from 0 to 100 on fundamentals (growth, profitability, valuation, balance sheet, analyst views, momentum)
- nudges each score by the **news ripples** moving markets, such as *crude up → producers and tankers up, paint makers and airlines squeezed*
- lets you **pin** stocks and see how they've moved since the day you pinned them

**Live app:** https://kirubakarandevaprasad.github.io/Stock-updates-Daily/

## How it works

```
GitHub Actions (weekdays, 22:17 UTC)
      │
      ▼
scripts/update_data.py ──► Yahoo Finance (yfinance): 6-month prices, fundamentals, analyst targets, headlines
      │                     config/themes.json: news themes, their market driver and each stock's sensitivity
      ▼
data/latest.json ──► index.html (GitHub Pages) renders the dashboard
```

| File | What it does |
|---|---|
| `index.html` | The app. Plain HTML, CSS and JS, no build step. Pins are saved in your browser. |
| `scripts/update_data.py` | Fetches the data, scores every stock, writes `data/latest.json`. |
| `config/themes.json` | The news themes. Each one has a live driver (crude, natural gas, 10-year yield, chip ETF, gold, defense ETF), a ripple chain and per-stock sensitivities. |
| `config/watchlist.txt` | Extra tickers to track. Add one per line. |
| `.github/workflows/daily-update.yml` | Runs the update on weekdays and commits fresh data. |

## The setup score

| Part | Max | Built from |
|---|---|---|
| Growth | 20 | Revenue and earnings growth, year over year |
| Profitability | 15 | Profit margin, return on equity |
| Valuation | 15 | Forward P/E, plus a bonus when forward P/E is below trailing P/E |
| Balance sheet | 10 | Debt/equity, current ratio |
| Analysts | 20 | Average target vs price, consensus rating |
| Momentum | 10 | Price vs 50- and 100-day averages |
| News tailwind | ±10 | Sum over linked themes of (driver's 1-month move ÷ theme scale) × the stock's sensitivity |

Missing data counts as neutral. Labels: 70+ Strong setup · 55–69 Promising · 40–54 Mixed · under 40 Shaky.

## Updating it

- **Run an update now:** Actions → *Daily market update* → *Run workflow*.
- **Track more stocks:** add tickers to `config/watchlist.txt`.
- **Change a theme or add one:** edit `config/themes.json`. `scale` is the 1-month driver move (in %) that counts as a full push. `sensitivity` runs from about −1.5 (hurt) to +1.5 (helped).
- **Run locally:** `pip install -r requirements.txt && python scripts/update_data.py`, then serve the folder (`python -m http.server`) and open `http://localhost:8000`.

## Disclaimer

This is a research and learning project, not investment advice. Scores are a mechanical screen of public data from Yahoo Finance, which is delayed and sometimes incomplete. Low-priced stocks are volatile and can be thinly traded. Do your own research, and talk to a licensed financial advisor before investing.
