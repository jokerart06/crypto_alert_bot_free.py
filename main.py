from __future__ import annotations

import argparse
import logging
import os
import time
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

LOGGER = logging.getLogger("crypto_alert_bot_free")

FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr"
KLINES_URL = "https://api.binance.com/api/v3/klines"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_GET_UPDATES = "https://api.telegram.org/bot{token}/getUpdates"

START_TIME = datetime.now().astimezone()
LAST_ALERT_TIME: datetime | None = None
NEXT_ALERT_TIME: datetime | None = None
WATCHLIST: list[str] = []
CHAT_IDS: list[str] = []


class MarketDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class MarketData:
    symbol: str
    interval: str
    price: float
    change_24h: float
    funding_percent: float
    ema20: float
    ema50: float
    rsi14: float
    volume_ratio: float


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def get_json(url: str, params: dict | None = None, timeout: float = 15) -> Any:
    response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def calculate_ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"Need at least {period} values for EMA")
    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for value in values[period:]:
        ema = (value - ema) * multiplier + ema
    return ema


def calculate_rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        raise ValueError(f"Need at least {period + 1} values for RSI")
    gains, losses = [], []
    for prev, curr in zip(values, values[1:]):
        change = curr - prev
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = ((avg_gain * (period - 1)) + g) / period
        avg_loss = ((avg_loss * (period - 1)) + l) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def _parse_klines(payload: Any) -> tuple[list[float], list[float]]:
    if not isinstance(payload, list):
        raise MarketDataError("Invalid klines")
    closes, volumes = [], []
    for c in payload:
        if not isinstance(c, list) or len(c) < 6:
            raise MarketDataError("Bad candle")
        closes.append(float(c[4]))
        volumes.append(float(c[5]))
    return closes, volumes


def get_market_data(symbol: str, interval: str, timeout: float) -> MarketData:
    try:
        funding_percent = 0.0
        try:
            fp = get_json(FUNDING_URL, params={"symbol": symbol}, timeout=timeout)
            if isinstance(fp, dict):
                funding_percent = float(fp.get("lastFundingRate", 0)) * 100
        except Exception as e:
            LOGGER.warning("Funding unavailable: %s", e)

        ticker = get_json(TICKER_URL, params={"symbol": symbol}, timeout=timeout)
        klines = get_json(KLINES_URL, params={"symbol": symbol, "interval": interval, "limit": 100}, timeout=timeout)

        if not isinstance(ticker, dict):
            raise MarketDataError("Invalid ticker")

        closes, volumes = _parse_klines(klines)
        if len(volumes) < 21:
            raise MarketDataError("Not enough candles")

        avg_vol = sum(volumes[-21:-1]) / 20
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0.0

        return MarketData(
            symbol=symbol,
            interval=interval,
            price=float(ticker["lastPrice"]),
            change_24h=float(ticker["priceChangePercent"]),
            funding_percent=funding_percent,
            ema20=calculate_ema(closes, 20),
            ema50=calculate_ema(closes, 50),
            rsi14=calculate_rsi(closes, 14),
            volume_ratio=vol_ratio,
        )
    except MarketDataError:
        raise
    except Exception as e:
        raise MarketDataError(f"Market data error: {e}") from e


def get_hot_coins(timeout: float = 12) -> tuple[list, list]:
    try:
        data = get_json(TICKER_URL, timeout=timeout)
        usdt = [
            t for t in data
            if isinstance(t, dict)
            and str(t.get("symbol", "")).endswith("USDT")
            and not any(x in t["symbol"] for x in ("UPUSDT", "DOWNUSDT", "BULL", "BEAR"))
        ]
        gainers = sorted(usdt, key=lambda x: float(x.get("priceChangePercent", 0)), reverse=True)[:10]
        top_gainers = [(t["symbol"], float(t["priceChangePercent"])) for t in gainers]

        by_vol = sorted(usdt, key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)[:8]
        top_vol = [(t["symbol"], float(t["quoteVolume"])) for t in by_vol]
        return top_gainers, top_vol
    except Exception as e:
        LOGGER.warning("Hot coins error: %s", e)
        return [], []


def get_whale_transactions(min_btc: float = 50, max_btc: float = 3000, hours: int = 8) -> list[dict]:
    """
    Best-effort free whale data.
    Uses a public source and filters 50–3000 BTC.
    Returns empty list on failure.
    """
    whales = []
    try:
        # Using Blockchair-style public recent large txs approximation
        # Note: Free endpoints are limited; this is best-effort
        url = "https://api.blockchair.com/bitcoin/transactions"
        params = {
            "q": f"output_total({int(min_btc * 1e8)}..{int(max_btc * 1e8)})",
            "limit": 20,
            "s": "time(desc)",
        }
        data = get_json(url, params=params, timeout=12)
        context = data.get("context", {})
        rows = data.get("data", [])

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        for tx in rows:
            try:
                # Blockchair returns time as string or unix
                tx_time = tx.get("time")
                if isinstance(tx_time, str):
                    tx_dt = datetime.fromisoformat(tx_time.replace("Z", "+00:00"))
                else:
                    tx_dt = datetime.fromtimestamp(tx_time, tz=timezone.utc)

                if tx_dt < cutoff:
                    continue

                amount_btc = float(tx.get("output_total", 0)) / 1e8
                if not (min_btc <= amount_btc <= max_btc):
                    continue

                # Simplified from/to (Blockchair structure varies)
                inputs = tx.get("inputs", []) or []
                outputs = tx.get("outputs", []) or []

                from_addr = "Unknown"
                to_addr = "Unknown"
                if inputs:
                    from_addr = str(inputs[0].get("recipient", inputs[0].get("address", "Unknown")))[:12] + "..."
                if outputs:
                    to_addr = str(outputs[0].get("recipient", outputs[0].get("address", "Unknown")))[:12] + "..."

                whales.append({
                    "amount": amount_btc,
                    "from": from_addr,
                    "to": to_addr,
                    "time": tx_dt.strftime("%H:%M UTC"),
                })
            except Exception:
                continue

        return whales[:8]  # max 8 entries
    except Exception as e:
        LOGGER.warning("Whale data unavailable: %s", e)
        return []


def score_market(data: MarketData) -> tuple[int, str]:
    score = 0
    if data.ema20 > data.ema50:
        score += 1
    if 45 <= data.rsi14 <= 65:
        score += 1
    if data.funding_percent < 0:
        score += 1
    if data.volume_ratio >= 1.0:
        score += 1

    if score >= 3 and data.ema20 > data.ema50 and data.rsi14 < 70:
        return score, "BUY WATCH"
    if score <= 1:
        return score, "SELL/WAIT"
    return score, "WAIT"


def get_uptime_str() -> str:
    uptime = datetime.now().astimezone() - START_TIME
    days = uptime.days
    hours, rem = divmod(uptime.seconds, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


def analyze_trend(data: MarketData) -> list[str]:
    lines = ["<b>📊 TREND ANALYZER</b>"]

    ema_diff = ((data.ema20 - data.ema50) / data.ema50 * 100) if data.ema50 else 0
    if data.ema20 > data.ema50:
        if ema_diff > 1.5:
            t = "Strong bullish structure"
        elif ema_diff > 0.4:
            t = "Bullish structure"
        else:
            t = "Mild bullish bias"
    else:
        if ema_diff < -1.5:
            t = "Strong bearish structure"
        elif ema_diff < -0.4:
            t = "Bearish structure"
        else:
            t = "Mild bearish bias"
    lines.append(f"• Structure: {t}")

    if data.change_24h > 5:
        m = "Strong positive momentum"
    elif data.change_24h > 1.5:
        m = "Positive momentum"
    elif data.change_24h > -1.5:
        m = "Neutral / consolidating"
    elif data.change_24h > -5:
        m = "Negative momentum"
    else:
        m = "Strong negative momentum"
    lines.append(f"• 24h Momentum: {m} ({data.change_24h:+.2f}%)")

    if data.rsi14 >= 70:
        r = "Overbought – caution"
    elif data.rsi14 >= 60:
        r = "Upper neutral – strength"
    elif data.rsi14 >= 45:
        r = "Healthy neutral"
    elif data.rsi14 >= 30:
        r = "Lower neutral – weakness"
    else:
        r = "Oversold – possible bounce zone"
    lines.append(f"• RSI: {r} ({data.rsi14:.1f})")

    if data.volume_ratio >= 1.5:
        v = "Strong volume confirmation"
    elif data.volume_ratio >= 1.0:
        v = "Volume supporting the move"
    else:
        v = "Low / average volume"
    lines.append(f"• Volume: {v}")

    points = 0
    if data.ema20 > data.ema50:
        points += 1
    if data.change_24h > 1:
        points += 1
    if 45 <= data.rsi14 <= 65:
        points += 1
    if data.volume_ratio >= 1.0:
        points += 1

    if points >= 3:
        bias = "Short-term bias appears cautiously bullish"
    elif points <= 1:
        bias = "Short-term bias appears cautiously bearish / weak"
    else:
        bias = "Short-term bias is mixed / neutral"
    lines.append(f"• Overall: {bias}")
    lines.append("<i>Based on recent technical data only. Not financial advice.</i>")
    return lines


def build_message(data: MarketData) -> str:
    timestamp = datetime.now().astimezone()
    score, signal = score_market(data)
    uptime = get_uptime_str()

    change_sign = "+" if data.change_24h >= 0 else ""
    trend = "BULLISH" if data.ema20 > data.ema50 else "BEARISH"

    lines = [
        f"<b>PRO MARKET CHECKLIST - {timestamp.strftime('%b %d %I:%M %p')}</b>",
        "",
        f"<b>{data.symbol}</b>: ${data.price:,.2f} ({change_sign}{data.change_24h:.2f}% 24h)",
        f"Trend: {trend} | EMA20 ${data.ema20:,.0f} / EMA50 ${data.ema50:,.0f}",
        f"RSI: {data.rsi14:.1f} | Funding: {data.funding_percent:.4f}% | Vol: {data.volume_ratio:.2f}x",
        "",
        f"<b>Score: {score}/4</b>  →  <b>{signal}</b>",
        "",
    ]

    lines.extend(analyze_trend(data))
    lines.append("")

    # Hot coins
    gainers, volumes = get_hot_coins()
    lines.append("<b>——— HOT COINS ———</b>")
    if gainers:
        lines.append("<b>Top Gainers:</b>")
        for i, (s, c) in enumerate(gainers[:7], 1):
            lines.append(f"{i}. {s} {c:+.1f}%")
    if volumes:
        lines.append("<b>Top Volume:</b>")
        for i, (s, v) in enumerate(volumes[:5], 1):
            lines.append(f"{i}. {s} ${v/1e6:,.0f}M")

    # Whale Alert
    lines.append("")
    lines.append("<b>——— WHALE ALERT (50–3000 BTC) ———</b>")
    whales = get_whale_transactions(min_btc=50, max_btc=3000, hours=8)
    if whales:
        for w in whales:
            lines.append(f"• {w['amount']:.1f} BTC | {w['from']} → {w['to']} | {w['time']}")
    else:
        lines.append("No large transactions found in last 8h (or data temporarily unavailable)")

    if WATCHLIST:
        lines.append("")
        lines.append(f"<b>Watchlist:</b> {', '.join(WATCHLIST)}")

    lines += [
        "",
        f"<i>Uptime: {uptime}</i>",
        "<i>Educational only — not financial advice.</i>",
        f"<i>{timestamp.strftime('%Y-%m-%d %H:%M')}</i>",
    ]
    return "\n".join(lines)


def send_telegram(message: str) -> None:
    token = os.getenv("BOT_TOKEN")
    if not token or not CHAT_IDS:
        raise ValueError("BOT_TOKEN and CHAT_ID required")

    if len(message) > 4090:
        message = message[:4000] + "\n\n...(truncated)"

    for chat_id in CHAT_IDS:
        try:
            r = requests.post(
                TELEGRAM_URL.format(token=token),
                data={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
                timeout=12,
            )
            if r.ok:
                LOGGER.info("Sent to %s", chat_id)
            else:
                LOGGER.warning("Failed to send to %s: %s", chat_id, r.text[:200])
        except Exception as e:
            LOGGER.warning("Send error to %s: %s", chat_id, e)


def run_once(dry_run: bool = False) -> None:
    global LAST_ALERT_TIME, NEXT_ALERT_TIME
    symbol = os.getenv("SYMBOL", "BTCUSDT").upper()
    interval = os.getenv("CANDLE_INTERVAL", "1h")
    timeout = env_float("REQUEST_TIMEOUT_SECONDS", 15)

    data = get_market_data(symbol, interval, timeout)
    message = build_message(data)

    if dry_run:
        print(message)
        return

    send_telegram(message)
    LAST_ALERT_TIME = datetime.now().astimezone()
    hours = env_float("RUN_INTERVAL_HOURS", 6)
    NEXT_ALERT_TIME = LAST_ALERT_TIME + timedelta(hours=hours)


def handle_command(text: str, from_chat_id: str) -> str | None:
    global WATCHLIST, CHAT_IDS
    lower = text.strip().lower()
    is_admin = from_chat_id == CHAT_IDS[0] if CHAT_IDS else False

    if lower in ("/start", "start"):
        return (
            "<b>Crypto Alert Bot</b>\n\n"
            "/uptime /refresh /status\n"
            "/watchlist /add SYMBOL /remove SYMBOL\n"
            "/subscribers (admin)\n"
            "/adduser CHAT_ID (admin)\n"
            "/removeuser CHAT_ID (admin)"
        )

    if lower in ("/uptime", "uptime"):
        return f"Uptime: <b>{get_uptime_str()}</b>"

    if lower in ("/refresh", "/now", "refresh", "now"):
        try:
            run_once()
            return "Full report sent."
        except Exception as e:
            return f"Error: {e}"

    if lower in ("/status", "status"):
        last = LAST_ALERT_TIME.strftime("%Y-%m-%d %H:%M") if LAST_ALERT_TIME else "Never"
        nxt = NEXT_ALERT_TIME.strftime("%Y-%m-%d %H:%M") if NEXT_ALERT_TIME else "—"
        return f"Uptime: {get_uptime_str()}\nLast: {last}\nNext: {nxt}\nSubscribers: {len(CHAT_IDS)}"

    if lower in ("/watchlist", "watchlist"):
        return "Watchlist:\n" + ("\n".join(f"• {s}" for s in WATCHLIST) if WATCHLIST else "Empty")

    if lower.startswith(("/add ", "add ")):
        sym = text.split(maxsplit=1)[1].strip().upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        if sym not in WATCHLIST:
            WATCHLIST.append(sym)
        return f"Added {sym}"

    if lower.startswith(("/remove ", "remove ")):
        sym = text.split(maxsplit=1)[1].strip().upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        if sym in WATCHLIST:
            WATCHLIST.remove(sym)
            return f"Removed {sym}"
        return "Not in watchlist"

    # Admin commands
    if lower in ("/subscribers", "subscribers"):
        if not is_admin:
            return "Admin only."
        return "Subscribers:\n" + "\n".join(CHAT_IDS)

    if lower.startswith(("/adduser ", "adduser ")):
        if not is_admin:
            return "Admin only."
        new_id = text.split(maxsplit=1)[1].strip()
        if new_id not in CHAT_IDS:
            CHAT_IDS.append(new_id)
            return f"Added subscriber {new_id}"
        return "Already subscribed"

    if lower.startswith(("/removeuser ", "removeuser ")):
        if not is_admin:
            return "Admin only."
        rid = text.split(maxsplit=1)[1].strip()
        if rid in CHAT_IDS and rid != CHAT_IDS[0]:
            CHAT_IDS.remove(rid)
            return f"Removed {rid}"
        return "Cannot remove or not found"

    return None


def telegram_listener():
    token = os.getenv("BOT_TOKEN")
    if not token or not CHAT_IDS:
        return
    offset = 0
    LOGGER.info("Listener started")
    while True:
        try:
            r = requests.get(TELEGRAM_GET_UPDATES.format(token=token),
                             params={"offset": offset, "timeout": 30}, timeout=35)
            data = r.json()
            if not data.get("ok"):
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if not msg:
                    continue
                chat_id = str(msg["chat"]["id"])
                text = msg.get("text", "")
                if text:
                    reply = handle_command(text, chat_id)
                    if reply:
                        # reply only to the sender
                        requests.post(TELEGRAM_URL.format(token=token),
                                      data={"chat_id": chat_id, "text": reply, "parse_mode": "HTML"},
                                      timeout=10)
        except Exception as e:
            LOGGER.warning("Listener error: %s", e)
            time.sleep(10)


def load_config():
    global CHAT_IDS, WATCHLIST
    raw_ids = os.getenv("CHAT_ID", "")
    CHAT_IDS = [x.strip() for x in raw_ids.split(",") if x.strip()]
    raw_sym = os.getenv("SYMBOLS", "")
    WATCHLIST = [s.strip().upper() for s in raw_sym.split(",") if s.strip()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
    load_config()

    if not CHAT_IDS:
        raise ValueError("CHAT_ID is required")

    if args.dry_run or args.once:
        run_once(dry_run=args.dry_run)
        return

    threading.Thread(target=telegram_listener, daemon=True).start()
    hours = env_float("RUN_INTERVAL_HOURS", 6)
    LOGGER.info("Bot started | every %.1f h | subscribers: %d", hours, len(CHAT_IDS))

    try:
        run_once()
    except Exception as e:
        LOGGER.error("First run failed: %s", e)

    while True:
        time.sleep(hours * 3600)
        try:
            run_once()
        except Exception as e:
            LOGGER.error("Run failed: %s", e)


if __name__ == "__main__":
    main()
