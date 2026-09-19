from __future__ import annotations

import argparse
import logging
import os
import time
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
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


def get_json(url: str, params: dict[str, str | int] | None = None, timeout: float = 15) -> Any:
    response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def calculate_ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"At least {period} values are required for EMA")
    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for value in values[period:]:
        ema = (value - ema) * multiplier + ema
    return ema


def calculate_rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        raise ValueError(f"At least {period + 1} values are required for RSI")
    gains, losses = [], []
    for previous, current in zip(values, values[1:]):
        change = current - previous
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = ((average_gain * (period - 1)) + gain) / period
        average_loss = ((average_loss * (period - 1)) + loss) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    return 100 - (100 / (1 + average_gain / average_loss))


def _parse_klines(payload: Any) -> tuple[list[float], list[float]]:
    if not isinstance(payload, list):
        raise MarketDataError("Invalid candle data")
    closes, volumes = [], []
    for candle in payload:
        if not isinstance(candle, list) or len(candle) < 6:
            raise MarketDataError("Incomplete candle")
        closes.append(float(candle[4]))
        volumes.append(float(candle[5]))
    return closes, volumes


def get_market_data(symbol: str, interval: str, timeout: float) -> MarketData:
    try:
        funding_percent = 0.0
        try:
            funding_payload = get_json(FUNDING_URL, params={"symbol": symbol}, timeout=timeout)
            if isinstance(funding_payload, dict):
                funding_percent = float(funding_payload.get("lastFundingRate", 0)) * 100
        except Exception as e:
            LOGGER.warning("Funding rate unavailable: %s", e)

        ticker_payload = get_json(TICKER_URL, params={"symbol": symbol}, timeout=timeout)
        klines_payload = get_json(
            KLINES_URL,
            params={"symbol": symbol, "interval": interval, "limit": 100},
            timeout=timeout,
        )

        if not isinstance(ticker_payload, dict):
            raise MarketDataError("Invalid ticker data")

        closes, volumes = _parse_klines(klines_payload)
        if len(volumes) < 21:
            raise MarketDataError("Not enough candles")

        avg_vol = sum(volumes[-21:-1]) / 20
        volume_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0.0

        return MarketData(
            symbol=symbol,
            interval=interval,
            price=float(ticker_payload["lastPrice"]),
            change_24h=float(ticker_payload["priceChangePercent"]),
            funding_percent=funding_percent,
            ema20=calculate_ema(closes, 20),
            ema50=calculate_ema(closes, 50),
            rsi14=calculate_rsi(closes, 14),
            volume_ratio=volume_ratio,
        )
    except MarketDataError:
        raise
    except Exception as exc:
        raise MarketDataError(f"Could not fetch market data: {exc}") from exc


def get_hot_coins(timeout: float = 15) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    try:
        data = get_json(TICKER_URL, timeout=timeout)
        usdt = [
            t for t in data
            if isinstance(t, dict)
            and str(t.get("symbol", "")).endswith("USDT")
            and not str(t["symbol"]).endswith(("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"))
        ]

        gainers = sorted(usdt, key=lambda x: float(x.get("priceChangePercent", 0)), reverse=True)[:10]
        top_gainers = [(t["symbol"], float(t["priceChangePercent"])) for t in gainers]

        by_vol = sorted(usdt, key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)[:10]
        top_volume = [(t["symbol"], float(t["quoteVolume"])) for t in by_vol]

        return top_gainers, top_volume
    except Exception as e:
        LOGGER.warning("Hot coins unavailable: %s", e)
        return [], []


def score_market(data: MarketData) -> tuple[int, str]:
    score = 0
    trend_good = data.ema20 > data.ema50
    if trend_good:
        score += 1
    if 45 <= data.rsi14 <= 65:
        score += 1
    if data.funding_percent < 0:
        score += 1
    if data.volume_ratio >= 1.0:
        score += 1

    if score >= 3 and trend_good and data.rsi14 < 70:
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
    """Create a careful, educational trend analysis."""
    lines = ["<b>📊 TREND ANALYZER</b>"]

    # 1. Trend structure
    ema_diff_pct = ((data.ema20 - data.ema50) / data.ema50) * 100 if data.ema50 else 0

    if data.ema20 > data.ema50:
        if ema_diff_pct > 1.5:
            trend_text = "Strong bullish structure (EMA20 well above EMA50)"
        elif ema_diff_pct > 0.4:
            trend_text = "Bullish structure (EMA20 above EMA50)"
        else:
            trend_text = "Mild bullish bias (EMAs close)"
    else:
        if ema_diff_pct < -1.5:
            trend_text = "Strong bearish structure (EMA20 well below EMA50)"
        elif ema_diff_pct < -0.4:
            trend_text = "Bearish structure (EMA20 below EMA50)"
        else:
            trend_text = "Mild bearish bias (EMAs close)"

    lines.append(f"• Structure: {trend_text}")

    # 2. Momentum (24h)
    if data.change_24h > 5:
        mom = "Strong positive momentum"
    elif data.change_24h > 1.5:
        mom = "Positive momentum"
    elif data.change_24h > -1.5:
        mom = "Neutral / consolidating"
    elif data.change_24h > -5:
        mom = "Negative momentum"
    else:
        mom = "Strong negative momentum"

    lines.append(f"• 24h Momentum: {mom} ({data.change_24h:+.2f}%)")

    # 3. RSI
    if data.rsi14 >= 70:
        rsi_text = "Overbought zone — caution for long entries"
    elif data.rsi14 >= 60:
        rsi_text = "Upper neutral — strength present"
    elif data.rsi14 >= 45:
        rsi_text = "Healthy neutral zone"
    elif data.rsi14 >= 30:
        rsi_text = "Lower neutral — weakness present"
    else:
        rsi_text = "Oversold zone — possible bounce area"

    lines.append(f"• RSI: {rsi_text} ({data.rsi14:.1f})")

    # 4. Volume
    if data.volume_ratio >= 1.5:
        vol_text = "Strong volume confirmation"
    elif data.volume_ratio >= 1.0:
        vol_text = "Volume supporting the move"
    elif data.volume_ratio >= 0.7:
        vol_text = "Average volume"
    else:
        vol_text = "Low volume — move lacks conviction"

    lines.append(f"• Volume: {vol_text}")

    # 5. Overall bias (careful language)
    bull_points = 0
    if data.ema20 > data.ema50:
        bull_points += 1
    if data.change_24h > 1:
        bull_points += 1
    if 45 <= data.rsi14 <= 65:
        bull_points += 1
    if data.volume_ratio >= 1.0:
        bull_points += 1

    if bull_points >= 3:
        bias = "Short-term bias appears cautiously bullish"
    elif bull_points <= 1:
        bias = "Short-term bias appears cautiously bearish / weak"
    else:
        bias = "Short-term bias is mixed / neutral"

    lines.append(f"• Overall: {bias}")
    lines.append("")
    lines.append("<i>Based on recent technical data only. Not financial advice.</i>")

    return lines


def build_message(data: MarketData, now: datetime | None = None) -> tuple[str, int, str]:
    timestamp = now or datetime.now().astimezone()
    score, signal = score_market(data)
    uptime_str = get_uptime_str()

    change_sign = "+" if data.change_24h >= 0 else ""
    trend_status = "BULLISH" if data.ema20 > data.ema50 else "BEARISH"

    if 45 <= data.rsi14 <= 65:
        rsi_status = "GOOD"
    elif data.rsi14 > 70:
        rsi_status = "OVERBOUGHT"
    elif data.rsi14 < 30:
        rsi_status = "OVERSOLD"
    else:
        rsi_status = "NEUTRAL"

    if data.funding_percent < 0:
        funding_status = "GOOD"
    elif data.funding_percent > 0.10:
        funding_status = "BAD"
    else:
        funding_status = "NEUTRAL"

    volume_status = "GOOD" if data.volume_ratio >= 1.0 else "NEUTRAL"

    top_gainers, top_volume = get_hot_coins()

    lines = [
        f"<b>PRO MARKET CHECKLIST - {timestamp.strftime('%b %d %I:%M %p')}</b>",
        "",
        f"<b>{data.symbol}</b>: ${data.price:,.2f} ({change_sign}{data.change_24h:.2f}% 24h)",
        "",
        f"Trend: {trend_status} (EMA20 ${data.ema20:,.2f} / EMA50 ${data.ema50:,.2f})",
        f"RSI 14: {data.rsi14:.1f} {rsi_status}",
        f"Funding: {data.funding_percent:.4f}% {funding_status}",
        f"Volume: {data.volume_ratio:.2f}x {volume_status}",
        "",
        f"<b>Score: {score}/4</b>  |  Strength: {score*25}%",
        f"<b>Signal: {signal}</b>",
        "",
    ]

    # Add Trend Analyzer
    lines.extend(analyze_trend(data))
    lines.append("")

    # Hot coins
    lines.append("<b>——— HOT COINS ———</b>")
    if top_gainers:
        lines.append("<b>Top Gainers:</b>")
        for i, (sym, ch) in enumerate(top_gainers[:8], 1):  # limit to 8 to save space
            lines.append(f"{i}. {sym} {ch:+.1f}%")
    lines.append("")
    if top_volume:
        lines.append("<b>Top Volume:</b>")
        for i, (sym, vol) in enumerate(top_volume[:6], 1):
            lines.append(f"{i}. {sym} ${vol/1_000_000:,.0f}M")

    if WATCHLIST:
        lines.append("")
        lines.append(f"<b>Watchlist:</b> {', '.join(WATCHLIST)}")

    lines += [
        "",
        f"<i>Bot Uptime: {uptime_str}</i>",
        "<i>Educational only — not financial advice.</i>",
        f"<i>{timestamp.strftime('%Y-%m-%d %H:%M')}</i>",
    ]

    return "\n".join(lines), score, signal


def send_telegram(message: str, timeout: float = 12) -> None:
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    if not token or not chat_id:
        raise ValueError("BOT_TOKEN and CHAT_ID required")

    if len(message) > 4090:
        message = message[:4000] + "\n\n...(truncated)"

    response = requests.post(
        TELEGRAM_URL.format(token=token),
        data={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
        timeout=timeout,
    )
    result = response.json() if response.content else {}
    if not response.ok or not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram error"))
    LOGGER.info("Message sent")


def run_once(*, dry_run: bool = False) -> None:
    global LAST_ALERT_TIME, NEXT_ALERT_TIME

    symbol = os.getenv("SYMBOL", "BTCUSDT").upper()
    interval = os.getenv("CANDLE_INTERVAL", "1h")
    timeout = env_float("REQUEST_TIMEOUT_SECONDS", 15)

    data = get_market_data(symbol, interval, timeout)
    message, score, signal = build_message(data)

    if dry_run:
        print(message)
        return

    send_telegram(message)
    LAST_ALERT_TIME = datetime.now().astimezone()
    hours = env_float("RUN_INTERVAL_HOURS", 6)
    NEXT_ALERT_TIME = LAST_ALERT_TIME + timedelta(hours=hours)


def handle_command(text: str) -> str | None:
    global WATCHLIST
    lower = text.strip().lower()

    if lower in ("/start", "start"):
        return (
            "<b>Crypto Alert Bot Online</b>\n\n"
            "/uptime - Uptime\n"
            "/refresh or /now - Full report now\n"
            "/status - Status\n"
            "/watchlist - Show watchlist\n"
            "/add SYMBOL - Add to watchlist\n"
            "/remove SYMBOL - Remove\n"
            "/clearwatchlist\n"
            "/help"
        )

    if lower in ("/help", "help"):
        return "Commands: /uptime /refresh /status /watchlist /add /remove /clearwatchlist"

    if lower in ("/uptime", "uptime"):
        return f"<b>Uptime:</b> {get_uptime_str()}"

    if lower in ("/refresh", "/now", "refresh", "now"):
        try:
            run_once()
            return None
        except Exception as e:
            return f"Error: {e}"

    if lower in ("/status", "status"):
        uptime = get_uptime_str()
        last = LAST_ALERT_TIME.strftime("%Y-%m-%d %H:%M") if LAST_ALERT_TIME else "Never"
        nxt = NEXT_ALERT_TIME.strftime("%Y-%m-%d %H:%M") if NEXT_ALERT_TIME else "—"
        return f"<b>Status</b>\nUptime: {uptime}\nLast: {last}\nNext: {nxt}\nWatchlist: {', '.join(WATCHLIST) or 'Empty'}"

    if lower in ("/watchlist", "watchlist"):
        return f"<b>Watchlist:</b>\n" + ("\n".join(f"• {s}" for s in WATCHLIST) if WATCHLIST else "Empty")

    if lower.startswith(("/add ", "add ")):
        sym = text.split(maxsplit=1)[1].strip().upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        if sym in WATCHLIST:
            return f"{sym} already in watchlist"
        WATCHLIST.append(sym)
        return f"Added <b>{sym}</b>"

    if lower.startswith(("/remove ", "remove ")):
        sym = text.split(maxsplit=1)[1].strip().upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        if sym in WATCHLIST:
            WATCHLIST.remove(sym)
            return f"Removed <b>{sym}</b>"
        return f"{sym} not in watchlist"

    if lower in ("/clearwatchlist", "clearwatchlist"):
        WATCHLIST.clear()
        return "Watchlist cleared"

    return None


def telegram_listener():
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    if not token or not chat_id:
        return

    offset = 0
    LOGGER.info("Command listener started")

    while True:
        try:
            r = requests.get(
                TELEGRAM_GET_UPDATES.format(token=token),
                params={"offset": offset, "timeout": 30},
                timeout=35,
            )
            data = r.json()
            if not data.get("ok"):
                time.sleep(5)
                continue

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if not msg or str(msg["chat"]["id"]) != str(chat_id):
                    continue
                text = msg.get("text", "")
                if text:
                    reply = handle_command(text)
                    if reply:
                        send_telegram(reply)
        except Exception as e:
            LOGGER.warning("Listener error: %s", e)
            time.sleep(10)


def load_watchlist():
    global WATCHLIST
    raw = os.getenv("SYMBOLS", "")
    if raw:
        WATCHLIST = [s.strip().upper() for s in raw.split(",") if s.strip()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")

    load_watchlist()
    hours = env_float("RUN_INTERVAL_HOURS", 6)

    if args.dry_run or args.once:
        run_once(dry_run=args.dry_run)
        return

    threading.Thread(target=telegram_listener, daemon=True).start()
    LOGGER.info("Bot started | every %.1f hours", hours)

    try:
        run_once()
    except Exception as e:
        LOGGER.error("First alert failed: %s", e)

    while True:
        time.sleep(hours * 3600)
        try:
            run_once()
        except Exception as e:
            LOGGER.error("Alert failed: %s", e)


if __name__ == "__main__":
    main()
