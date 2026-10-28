#!/usr/bin/env python3
"""Public Upbit OHLCV scanner for 15 KRW markets; no account keys or orders.

Reads the latest fully closed 1H/4H/1D/1W candles, calculates indicators,
then writes data/latest.json and data/latest.md. Python standard library only.
"""

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

KST = timezone(timedelta(hours=9))
API_ROOT = "https://api.upbit.com/v1"
SYMBOLS = [
    "BTC", "XRP", "ETH", "SOL", "LINK", "ONDO", "HBAR", "XLM",
    "RENDER", "TAO", "AKT", "WLD", "GRT", "ENA", "AAVE",
]
FRAMES = {
    "1H": ("/candles/minutes/60", timedelta(minutes=60)),
    "4H": ("/candles/minutes/240", timedelta(minutes=240)),
    "1D": ("/candles/days", timedelta(days=1)),
    "1W": ("/candles/weeks", timedelta(weeks=1)),
}
REQUEST_GAP_SEC = 0.18
CANDLE_COUNT = 200


def api_get(path, params):
    url = API_ROOT + path + "?" + urlencode(params)
    last_error = None
    for attempt in range(5):
        try:
            req = Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "upbit-15-public-research/1.0",
            })
            with urlopen(req, timeout=25) as response:
                data = json.load(response)
            time.sleep(REQUEST_GAP_SEC)
            return data
        except HTTPError as exc:
            last_error = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except (URLError, TimeoutError) as exc:
            last_error = exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"API request failed: {path} {params}: {last_error}")


def as_utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def iso_kst(dt):
    return dt.astimezone(KST).isoformat(timespec="seconds")


def get_closed_candles(market, frame, now):
    path, span = FRAMES[frame]
    rows = api_get(path, {"market": market, "count": CANDLE_COUNT})
    if not isinstance(rows, list):
        raise ValueError(f"Invalid candle response: {market} {frame}")
    closed = []
    for raw in rows:
        start = as_utc(raw["candle_date_time_utc"])
        # The API can return a still-forming most recent bar; exclude it.
        if start + span > now - timedelta(seconds=10):
            continue
        closed.append({
            "start_utc": start,
            "end_utc": start + span,
            "o": float(raw["opening_price"]),
            "h": float(raw["high_price"]),
            "l": float(raw["low_price"]),
            "c": float(raw["trade_price"]),
            "v": float(raw["candle_acc_trade_volume"]),
            "turnover_krw": float(raw["candle_acc_trade_price"]),
        })
    closed.sort(key=lambda row: row["start_utc"])
    if len(closed) < 23:
        raise ValueError(f"Too few completed candles: {market} {frame} ({len(closed)})")
    return closed


def rounded(value, digits=2):
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def rsi_wilder(closes, period=14):
    if len(closes) <= period:
        return None
    changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gain = sum(max(0, c) for c in changes[:period]) / period
    loss = sum(max(0, -c) for c in changes[:period]) / period
    for c in changes[period:]:
        gain = (gain * (period - 1) + max(0, c)) / period
        loss = (loss * (period - 1) + max(0, -c)) / period
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100 - (100 / (1 + gain / loss))


def mfi(candles, period=14):
    if len(candles) <= period:
        return None
    positive = negative = 0.0
    for i in range(len(candles) - period, len(candles)):
        curr = candles[i]
        prev = candles[i - 1]
        tp = (curr["h"] + curr["l"] + curr["c"]) / 3
        prev_tp = (prev["h"] + prev["l"] + prev["c"]) / 3
        flow = tp * curr["v"]
        if tp > prev_tp:
            positive += flow
        elif tp < prev_tp:
            negative += flow
    if negative == 0:
        return 100.0 if positive > 0 else 50.0
    return 100 - 100 / (1 + positive / negative)


def parabolic_sar(candles, step=0.02, max_af=0.20):
    if len(candles) < 3:
        return None
    up = candles[1]["c"] >= candles[0]["c"]
    sar = min(candles[0]["l"], candles[1]["l"]) if up else max(candles[0]["h"], candles[1]["h"])
    ep = max(candles[0]["h"], candles[1]["h"]) if up else min(candles[0]["l"], candles[1]["l"])
    af = step
    for i in range(2, len(candles)):
        bar = candles[i]
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, candles[i-1]["l"], candles[i-2]["l"])
            if bar["l"] < sar:
                up, sar, ep, af = False, ep, bar["l"], step
            elif bar["h"] > ep:
                ep, af = bar["h"], min(max_af, af + step)
        else:
            sar = max(sar, candles[i-1]["h"], candles[i-2]["h"])
            if bar["h"] > sar:
                up, sar, ep, af = True, ep, bar["h"], step
            elif bar["l"] < ep:
                ep, af = bar["l"], min(max_af, af + step)
    return {"value": rounded(sar, 8), "side": "below" if up else "above"}


def swing_structure(candles, width=2):
    highs, lows = [], []
    for i in range(width, len(candles) - width):
        h = candles[i]["h"]
        l = candles[i]["l"]
        ns = candles[i-width:i] + candles[i+1:i+1+width]
        if all(h > item["h"] for item in ns):
            highs.append(h)
        if all(l < item["l"] for item in ns):
            lows.append(l)
    if len(highs) < 2 or len(lows) < 2:
        return "unconfirmed"
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "HH_HL"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "LH_LL"
    return "mixed"


def analyze(candles, now, frame):
    closes = [r["c"] for r in candles]
    last = candles[-1]
    before = candles[-2]
    volumes_before = [c["v"] for c in candles[-21:-1]]
    avg_volume = sum(volumes_before) / len(volumes_before)
    psar = parabolic_sar(candles)
    period = FRAMES[frame][1]
    # Prior 20 completed bars, excluding the latest, for resistance/support observation.
    previous = candles[-21:-1]
    return {
        "closed_at_kst": iso_kst(last["end_utc"]),
        "close_krw": last["c"],
        "previous_close_krw": before["c"],
        "change_pct": rounded((last["c"] / before["c"] - 1) * 100, 3) if before["c"] else None,
        "rsi14_wilder": rounded(rsi_wilder(closes)),
        "mfi14": rounded(mfi(candles)),
        "psar": psar,
        "sma20": rounded(sum(closes[-20:]) / 20, 6) if len(closes) >= 20 else None,
        "sma50": rounded(sum(closes[-50:]) / 50, 6) if len(closes) >= 50 else None,
        "volume": last["v"],
        "volume_ratio_to_previous20": rounded(last["v"] / avg_volume, 3) if avg_volume else None,
        "prior20_high_krw": max(r["h"] for r in previous),
        "prior20_low_krw": min(r["l"] for r in previous),
        "swing_structure": swing_structure(candles),
        "stale_periods": rounded((now - last["end_utc"]) / period, 2),
    }


def fmt_num(v):
    if v is None:
        return "n/a"
    if isinstance(v, int) or (isinstance(v, float) and v >= 100 and v.is_integer()):
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def md_table_rows(result):
    rows = []
    for symbol in SYMBOLS:
        market = result["markets"][symbol]
        h1 = market["frames"]["1H"]
        h4 = market["frames"]["4H"]
        spot = market["ticker"]
        rows.append(
            f"| {symbol} | {fmt_num(spot['trade_price_krw'])} | "
            f"{fmt_num(h1['rsi14_wilder'])} | {fmt_num(h1['mfi14'])} | "
            f"{fmt_num(h4['rsi14_wilder'])} | {fmt_num(h4['mfi14'])} | "
            f"{h4['swing_structure']} | {fmt_num(h1['volume_ratio_to_previous20'])} |"
        )
    return rows


def write_files(result):
    Path("data").mkdir(exist_ok=True)
    (Path("data") / "latest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    h1_close = result["markets"]["BTC"]["frames"]["1H"]["closed_at_kst"]
    h4_close = result["markets"]["BTC"]["frames"]["4H"]["closed_at_kst"]
    text = [
        "# Upbit 15-market public technical snapshot",
        "",
        f"Generated (KST): **{result['generated_at_kst']}**",
        f"Latest verified BTC completed 1H close: **{h1_close}**",
        f"Latest verified BTC completed 4H close: **{h4_close}**",
        "",
        "All RSI/MFI/PSAR/structure fields use completed candles only.",
        "Spot price is a separate API ticker observation; check the generation time.",
        "Some chart-indicator implementations can differ from Upbit app defaults.",
        "The swing structure is a simple two-sided pivot heuristic, not trading advice.",
        "No account balances, holdings, open orders, fills, or trades are accessed.",
        "",
        "| Market | Spot KRW | 1H RSI14 | 1H MFI14 | 4H RSI14 | 4H MFI14 | 4H swings | 1H volume / prior 20 |",
        "|---|---:|---:|---:|---:|---:|---|---:|",
        *md_table_rows(result),
        "",
        "## Per-market completed-bar timestamps and observed 20-bar ranges",
        "",
        "| Market | Frame | Closed at (KST) | Close KRW | Prior-20 high | Prior-20 low | PSAR side | SMA20 | SMA50 |",
        "|---|---|---|---:|---:|---:|---|---:|---:|",
    ]
    for symbol in SYMBOLS:
        frames = result["markets"][symbol]["frames"]
        for f in ("1H", "4H", "1D", "1W"):
            r = frames[f]
            text.append(
                f"| {symbol} | {f} | {r['closed_at_kst']} | {fmt_num(r['close_krw'])} | "
                f"{fmt_num(r['prior20_high_krw'])} | {fmt_num(r['prior20_low_krw'])} | "
                f"{r['psar']['side']} | {fmt_num(r['sma20'])} | {fmt_num(r['sma50'])} |"
            )
    text += [
        "",
        "## Readiness checks",
        "",
        "- Upbit market tickers and API data may update at slightly different instants.",
        "- Verify data freshness (`Generated (KST)`) before each ChatGPT brief.",
        "- Trend labels and previous high/low are observations, not proven supports.",
        "- Orders are NEVER placed or modified by this repository.",
        "",
    ]
    (Path("data") / "latest.md").write_text("\n".join(text), encoding="utf-8")


def main():
    now = datetime.now(timezone.utc)
    report = {
        "generated_at_utc": now.isoformat(timespec="seconds"),
        "generated_at_kst": iso_kst(now),
        "source": "Upbit public REST API",
        "symbols": SYMBOLS,
        "calculation_note": "Completed candles only; indicators may differ from Upbit UI settings.",
        "markets": {},
    }
    for symbol in SYMBOLS:
        name = "KRW-" + symbol
        frames = {}
        for frame in FRAMES:
            candles = get_closed_candles(name, frame, now)
            frames[frame] = analyze(candles, now, frame)
        report["markets"][symbol] = {"market": name, "frames": frames}
        print(f"[OK] {name}: 1H/4H/1D/1W", flush=True)
    tickers = api_get("/ticker", {"markets": ",".join("KRW-" + s for s in SYMBOLS)})
    prices = {t["market"]: t for t in tickers}
    for symbol in SYMBOLS:
        name = "KRW-" + symbol
        if name not in prices:
            raise RuntimeError(f"Ticker missing market {name}")
        ticker = prices[name]
        timestamp = ticker.get("timestamp") or ticker.get("trade_timestamp")
        ticker_time = (datetime.fromtimestamp(timestamp / 1000, timezone.utc) if timestamp else now)
        report["markets"][symbol]["ticker"] = {
            "trade_price_krw": ticker["trade_price"],
            "observed_at_kst": iso_kst(ticker_time),
            "collected_at_kst": iso_kst(datetime.now(timezone.utc)),
            "signed_change_rate": ticker.get("signed_change_rate"),
        }
    write_files(report)
    print(f"Saved data/latest.json and data/latest.md at {iso_kst(datetime.now(timezone.utc))}", flush=True)


if __name__ == "__main__":
    main()
