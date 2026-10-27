"""
Ripple Radar daily data job.

Pulls prices, fundamentals, analyst targets and headlines from Yahoo Finance
(via yfinance), scores every stock, sorts it into a price tier and works out
how today's news themes push on it. Writes data/latest.json for the web app.

Run locally:  pip install -r requirements.txt && python scripts/update_data.py
"""

from __future__ import annotations

import json
import math
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "themes.json"
WATCHLIST = ROOT / "config" / "watchlist.txt"
OUT = ROOT / "data" / "latest.json"

TIERS = [  # (id, label, low inclusive, high exclusive)
    ("low", "$1 – $10", 1.0, 10.0),
    ("mid", "$10 – $50", 10.0, 50.0),
    ("high", "$50+", 50.0, float("inf")),
]


# ---------------------------------------------------------------- helpers

def num(x):
    """Return a finite float or None."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def lerp(x, x0, x1, y0, y1):
    """Linear map x from [x0,x1] to [y0,y1], clamped."""
    if x is None:
        return None
    t = (x - x0) / (x1 - x0)
    t = max(0.0, min(1.0, t))
    return y0 + t * (y1 - y0)


def pct(a, b):
    a, b = num(a), num(b)
    if a is None or b in (None, 0):
        return None
    return (a / b - 1.0) * 100.0


def r(x, d=2):
    x = num(x)
    return None if x is None else round(x, d)


def tier_of(price):
    if price is None:
        return None
    for tid, _, lo, hi in TIERS:
        if lo <= price < hi:
            return tid
    return None  # sub-$1 stocks are left out on purpose


def parse_news(items, limit=3):
    """yfinance has shipped two news shapes; handle both."""
    out = []
    for it in items or []:
        c = it.get("content") if isinstance(it, dict) else None
        if c:
            title = c.get("title")
            url = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
            src = (c.get("provider") or {}).get("displayName")
            when = c.get("pubDate") or c.get("displayTime")
        else:
            title = it.get("title")
            url = it.get("link")
            src = it.get("publisher")
            ts = it.get("providerPublishTime")
            when = datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None
        if title and url:
            out.append({"title": title, "url": url, "source": src, "at": when})
        if len(out) >= limit:
            break
    return out


def fetch_rss(sym, limit=3):
    """Yahoo's public RSS headline feed: a fallback when yfinance returns no news."""
    url = "https://feeds.finance.yahoo.com/rss/2.0/headline?" + urllib.parse.urlencode(
        {"s": sym, "region": "US", "lang": "en-US"})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ripple-radar)"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        root = ET.fromstring(resp.read())
    out = []
    for item in root.findall("./channel/item"):
        title, link = item.findtext("title"), item.findtext("link")
        when = item.findtext("pubDate")
        try:
            when = parsedate_to_datetime(when).isoformat() if when else None
        except (TypeError, ValueError):
            pass
        if title and link:
            out.append({"title": title.strip(), "url": link.strip(), "source": "Yahoo Finance", "at": when})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- scoring

def score_stock(m, closes, tailwind):
    """Return (score 0-100, parts, reasons, risks). Missing data scores neutral."""
    price = m.get("price")
    parts, reasons, risks = {}, [], []

    # Growth (0-20)
    rg, eg = m.get("revenueGrowth"), m.get("earningsGrowth")
    g = (lerp(rg, -10, 30, 0, 12) if rg is not None else 6) + (lerp(eg, -20, 40, 0, 8) if eg is not None else 4)
    parts["growth"] = g / 20
    if rg is not None and rg >= 12:
        reasons.append(f"Revenue up {rg:.0f}% year over year")
    elif rg is not None and rg < -5:
        risks.append(f"Revenue down {abs(rg):.0f}% year over year")
    if eg is not None and eg >= 20:
        reasons.append(f"Earnings up {eg:.0f}% year over year")

    # Profitability (0-15)
    pm, roe = m.get("profitMargin"), m.get("roe")
    p = (lerp(pm, -10, 25, 0, 8) if pm is not None else 4) + (lerp(roe, 0, 30, 0, 7) if roe is not None else 3.5)
    parts["profit"] = p / 15
    if pm is not None and pm < 0:
        risks.append("Not profitable yet")
    elif pm is not None and pm >= 15:
        reasons.append(f"Fat {pm:.0f}% profit margin")

    # Valuation (0-15)
    fpe, tpe = m.get("forwardPE"), m.get("trailingPE")
    if fpe is None or fpe <= 0:
        v = 4.0
    elif fpe < 10:
        v = 14.0
    elif fpe < 20:
        v = lerp(fpe, 10, 20, 13, 9)
    elif fpe < 40:
        v = lerp(fpe, 20, 40, 9, 4)
    else:
        v = 2.0
    if fpe and tpe and 0 < fpe < tpe:
        v = min(15.0, v + 1.0)  # earnings expected to grow
    parts["value"] = v / 15
    if fpe and 0 < fpe < 12:
        reasons.append(f"Cheap at {fpe:.1f}x forward earnings")
    elif fpe and fpe > 45:
        risks.append(f"Pricey at {fpe:.0f}x forward earnings")

    # Balance sheet (0-10)
    de, cr = m.get("debtToEquity"), m.get("currentRatio")
    if de is None:
        b = 5.0
    elif de < 50:
        b = 10.0
    elif de < 150:
        b = lerp(de, 50, 150, 10, 5)
    else:
        b = lerp(de, 150, 400, 5, 0)
    if cr is not None and cr < 1:
        b = max(0.0, b - 2)
    parts["balance"] = b / 10
    if de is not None and de > 200:
        risks.append(f"Heavy debt (debt/equity {de / 100:.1f}x)")
    elif de is not None and de < 30:
        reasons.append("Light debt load")

    # Analysts (0-20)
    up = pct(m.get("targetMean"), price)
    rec = m.get("recMean")
    a = (lerp(up, -10, 40, 0, 12) if up is not None else 6) + (lerp(rec, 4, 1.5, 0, 8) if rec is not None else 4)
    parts["analysts"] = a / 20
    n = m.get("analysts") or 0
    if up is not None and up >= 15 and n >= 3:
        reasons.append(f"Analysts' avg target {up:.0f}% above price ({n} analysts)")
    elif up is not None and up < -5 and n >= 3:
        risks.append(f"Trading {abs(up):.0f}% above analysts' avg target")

    # Momentum (0-10)
    mo = 0.0
    if closes:
        last = closes[-1]
        ma50 = sum(closes[-50:]) / len(closes[-50:])
        ma100 = sum(closes[-100:]) / len(closes[-100:])
        mo += 5 if last > ma50 else 0
        mo += 5 if last > ma100 else 0
        if last > ma50 and last > ma100:
            reasons.append("Trading above its 50- and 100-day averages")
        elif last < ma50 and last < ma100:
            risks.append("Below its 50- and 100-day averages")
    else:
        mo = 5.0
    parts["momentum"] = mo / 10

    # News-theme tailwind (-10..+10)
    tw = max(-10.0, min(10.0, tailwind * 10))
    parts["tailwind"] = (tw + 10) / 20

    total = g + p + v + b + a + mo + tw
    total = max(0.0, min(100.0, total * 100 / 90))  # base max is 90 before tailwind
    return round(total), {k: round(x, 3) for k, x in parts.items()}, reasons[:4], risks[:3]


def label_for(score):
    if score >= 70:
        return "Strong setup"
    if score >= 55:
        return "Promising"
    if score >= 40:
        return "Mixed"
    return "Shaky"


# ---------------------------------------------------------------- fetching

def fetch_history(symbols):
    import yfinance as yf

    data = yf.download(symbols, period="6mo", interval="1d", auto_adjust=True,
                       group_by="ticker", progress=False, threads=True)
    out = {}
    for s in symbols:
        try:
            col = data[s]["Close"] if len(symbols) > 1 else data["Close"]
            closes = [float(x) for x in col.dropna().tolist()]
            dates = [d.strftime("%Y-%m-%d") for d in col.dropna().index]
            if closes:
                out[s] = (closes, dates)
        except Exception:
            pass
    return out


def fetch_info(sym):
    import yfinance as yf

    t = yf.Ticker(sym)
    info = {}
    for attempt in range(3):
        try:
            info = t.info or {}
            break
        except Exception:
            time.sleep(2 * (attempt + 1))
    news = []
    try:
        news = parse_news(t.news)
    except Exception:
        pass
    if not news and hasattr(t, "get_news"):
        try:
            news = parse_news(t.get_news(count=5))
        except Exception:
            pass
    if not news:
        try:
            news = fetch_rss(sym)
        except Exception:
            pass

    def p100(k):
        v = num(info.get(k))
        return None if v is None else v * 100

    dy = num(info.get("dividendYield"))
    if dy is not None and dy > 1:  # newer yfinance already returns percent
        dy = dy / 100
    return {
        "name": info.get("shortName") or info.get("longName") or sym,
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "marketCap": num(info.get("marketCap")),
        "trailingPE": num(info.get("trailingPE")),
        "forwardPE": num(info.get("forwardPE")),
        "revenueGrowth": p100("revenueGrowth"),
        "earningsGrowth": p100("earningsGrowth"),
        "profitMargin": p100("profitMargins"),
        "roe": p100("returnOnEquity"),
        "debtToEquity": num(info.get("debtToEquity")),
        "currentRatio": num(info.get("currentRatio")),
        "targetMean": num(info.get("targetMeanPrice")),
        "targetLow": num(info.get("targetLowPrice")),
        "targetHigh": num(info.get("targetHighPrice")),
        "recMean": num(info.get("recommendationMean")),
        "recKey": info.get("recommendationKey"),
        "analysts": int(num(info.get("numberOfAnalystOpinions")) or 0),
        "dividendYield": None if dy is None else dy * 100,
        "beta": num(info.get("beta")),
        "low52": num(info.get("fiftyTwoWeekLow")),
        "high52": num(info.get("fiftyTwoWeekHigh")),
        "earningsDate": info.get("earningsTimestamp"),
        "news": news,
    }


# ---------------------------------------------------------------- main

def build(history, infos, cfg, previous, skip=()):
    themes_out = []
    signal = {}
    for th in cfg["themes"]:
        d = th["driver"]
        h = history.get(d["symbol"])
        price = chg1d = chg1m = None
        spark = []
        if h:
            closes, _ = h
            price = closes[-1]
            chg1d = pct(closes[-1], closes[-2]) if len(closes) > 1 else None
            chg1m = pct(closes[-1], closes[-22]) if len(closes) > 22 else None
            spark = [round(x, 2) for x in closes[-66:]]
        sig = 0.0 if chg1m is None else max(-1.0, min(1.0, chg1m / d["scale"]))
        signal[th["id"]] = sig
        themes_out.append({
            "id": th["id"], "name": th["name"], "backdrop": th["backdrop"],
            "chain": th["chain"], "sensitivity": th["sensitivity"],
            "driver": {**d, "price": r(price), "chg1d": r(chg1d), "chg1m": r(chg1m), "spark": spark},
            "signal": round(sig, 3),
            "headlines": infos.get(d["symbol"], {}).get("news", []),
        })

    prev_scores = {s["t"]: s["score"] for s in (previous or {}).get("stocks", [])}
    stocks = []
    for sym, m in infos.items():
        if sym in skip or sym.startswith("^") or "=" in sym:
            continue
        h = history.get(sym)
        if not h:
            continue
        closes, _ = h
        price = closes[-1]
        tier = tier_of(price)
        if tier is None:
            continue
        m = {**m, "price": price}
        links = []
        tail = 0.0
        for th in cfg["themes"]:
            sens = th["sensitivity"].get(sym)
            if sens is None:
                continue
            push = sens * signal[th["id"]]
            tail += push
            links.append({"id": th["id"], "sens": sens, "push": round(push, 3)})
        tail = max(-1.0, min(1.0, tail))
        score, parts, reasons, risks = score_stock(m, closes, tail)
        if tail >= 0.25:
            top = max(links, key=lambda x: x["push"])
            name = next(t["name"] for t in cfg["themes"] if t["id"] == top["id"])
            reasons.insert(0, f"Tailwind from the {name} theme")
        elif tail <= -0.25:
            top = min(links, key=lambda x: x["push"])
            name = next(t["name"] for t in cfg["themes"] if t["id"] == top["id"])
            risks.insert(0, f"Headwind from the {name} theme")
        stocks.append({
            "t": sym, "name": m["name"], "sector": m["sector"], "industry": m["industry"],
            "price": r(price), "chg1d": r(pct(closes[-1], closes[-2]) if len(closes) > 1 else None),
            "chg1m": r(pct(closes[-1], closes[-22]) if len(closes) > 22 else None),
            "tier": tier, "spark": [round(x, 3) for x in closes[-66:]],
            "score": score, "scorePrev": prev_scores.get(sym), "label": label_for(score),
            "parts": parts, "reasons": reasons[:4], "risks": risks[:3],
            "themes": links, "tailwind": round(tail, 3),
            "metrics": {k: r(m.get(k)) for k in (
                "marketCap", "trailingPE", "forwardPE", "revenueGrowth", "earningsGrowth",
                "profitMargin", "roe", "debtToEquity", "currentRatio", "targetMean",
                "targetLow", "targetHigh", "recMean", "dividendYield", "beta", "low52", "high52")},
            "recKey": m.get("recKey"), "analysts": m.get("analysts"),
            "earningsDate": m.get("earningsDate"),
            "news": m.get("news", []),
        })
    stocks.sort(key=lambda s: -s["score"])
    return themes_out, stocks


def main():
    cfg = json.loads(CONFIG.read_text())
    tickers = set(cfg.get("extraTickers", []))
    for th in cfg["themes"]:
        tickers.update(th["sensitivity"].keys())
        for stage in th["chain"]:
            tickers.update(stage["tickers"])
    if WATCHLIST.exists():
        for line in WATCHLIST.read_text().splitlines():
            line = line.strip().upper()
            if line and not line.startswith("#"):
                tickers.add(line)
    drivers = {th["driver"]["symbol"] for th in cfg["themes"]}
    pulse_syms = [p["symbol"] for p in cfg["pulse"]]
    all_syms = sorted(tickers | drivers | set(pulse_syms))

    print(f"Fetching history for {len(all_syms)} symbols…")
    history = fetch_history(all_syms)
    print(f"  got {len(history)}")

    infos = {}
    for i, sym in enumerate(sorted(tickers | drivers)):
        try:
            infos[sym] = fetch_info(sym)
        except Exception as e:  # keep going; one bad ticker shouldn't sink the run
            print(f"  ! {sym}: {e}", file=sys.stderr)
            infos[sym] = {"name": sym, "news": []}
        if i % 10 == 9:
            print(f"  info {i + 1}/{len(tickers | drivers)}")
        time.sleep(0.4)

    previous = json.loads(OUT.read_text()) if OUT.exists() else None
    themes, stocks = build(history, infos, cfg, previous, skip=drivers - tickers)

    pulse = []
    for p in cfg["pulse"]:
        h = history.get(p["symbol"])
        if not h:
            continue
        closes, _ = h
        pulse.append({**p, "price": r(closes[-1]), "chg1d": r(pct(closes[-1], closes[-2]))})

    # Futures trade overnight, so take the session date from the S&P 500 itself.
    spx = history.get("^GSPC")
    market_date = spx[1][-1] if spx else max((ds[-1] for _, ds in history.values()), default=None)
    print(f"Headlines found for {sum(1 for v in infos.values() if v.get('news'))} of {len(infos)} symbols")
    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "marketDate": market_date,
        "backdropAsOf": cfg.get("asOf"),
        "tiers": [{"id": t, "label": l} for t, l, *_ in TIERS],
        "pulse": pulse, "themes": themes, "stocks": stocks,
        "source": "Yahoo Finance via yfinance (prices delayed; fundamentals as reported)",
    }
    if len(stocks) < 10:
        print("Too few stocks came back; keeping the previous file.", file=sys.stderr)
        sys.exit(1)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    print(f"Wrote {OUT} with {len(stocks)} stocks for {market_date}")


if __name__ == "__main__":
    main()
