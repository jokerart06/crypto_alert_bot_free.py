from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
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

# Watchlist (loaded from env, can be changed via Telegram)
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
        raise MarketDataError("Binance returned invalid candle data")
    closes, volumes = [], []
    for candle in payload:
        if not isinstance(candle, list) or len(candle) < 6:
            raise MarketDataError("Binance returned an incomplete candle")
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
            LOGGER.warning("Could not fetch funding rate (using 0.0): %s", e)

        ticker_payload = get_json(TICKER_URL, params={"symbol": symbol}, timeout=timeout)
        klines_payload = get_json(
            KLINES_URL,
            params={"symbol": symbol, "interval": interval, "limit": 100},
            timeout=timeout,
        )

        if not isinstance(ticker_payload, dict):
            raise MarketDataError("Binance returned invalid ticker data")

        closes, volumes = _parse_klines(klines_payload)
        if len(volumes) < 21:
            raise MarketDataError("Not enough candles for volume analysis")

        average_previous_volume = sum(volumes[-21:-1]) / 20
        volume_ratio = volumes[-1] / average_previous_volume if average_previous_volume > 0 else 0.0

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
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        raise MarketDataError(f"Could not fetch market data: {exc}") from exc


def get_hot_coins(timeout: float = 15) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """Return top 10 gainers and top 10 by volume (USDT pairs only)."""
    try:
        data = get_json(TICKER_URL, timeout=timeout)
        usdt_pairs = [
            t for t in data
            if isinstance(t, dict)
            and t.get("symbol", "").endswith("USDT")
            and not t["symbol"].endswith("UPUSDT")
            and not t["symbol"].endswith("DOWNUSDT")
        ]

        # Top Gainers
        gainers = sorted(
            usdt_pairs,
            key=lambda x: float(x.get("priceChangePercent", 0)),
            reverse=True
        )[:10]
        top_gainers = [(t["symbol"], float(t["priceChangePercent"])) for t in gainers]

        # Top Volume
        by_volume = sorted(
            usdt_pairs,
            key=lambda x: float(x.get("quoteVolume", 0)),
            reverse=True
        )[:10]
        top_volume = [(t["symbol"], float(t["quoteVolume"])) for t in by_volume]

        return top_gainers, top_volume
    except Exception as e:
        LOGGER.warning("Could not fetch hot coins: %s", e)
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
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


def build_message(data: MarketData, now: datetime | None = None) -> tuple[str, int, str]:
    timestamp = now or datetime.now().astimezone()
    score, signal = score_market(data)
    uptime_str = get_uptime_str()

    change_sign = "+" if data.change_24h >= 0 else ""
    trend_status = "BULLISH " if data.ema20 > data.ema50 else "BEARISH "

    if 45 <= data.rsi14 <= 65:
        rsi_status = "GOOD "
    elif data.rsi14 > 70:
        rsi_status = "OVERBOUGHT "
    elif data.rsi14 < 30:
        rsi_status = "OVERSOLD "
    else:
        rsi_status = "NEUTRAL "

    if data.funding_percent < 0:
        funding_status = "GOOD "
    elif data.funding_percent > 0.10:
        funding_status = "BAD "
    else:
        funding_status = "NEUTRAL "

    volume_status = "GOOD " if data.volume_ratio >= 1.0 else "NEUTRAL "

    # Hot coins
    top_gainers, top_volume = get_hot_coins()

    lines = [
        f"<b>PRO MARKET CHECKLIST - {timestamp.strftime('%b %d %I:%M %p')}</b>",
        "",
        f"<b>{data.symbol}</b>: ${data.price:,.2f} ({change_sign}{data.change_24h:.2f}% 24h)",
        "",
        f"Trend: {trend_status}(EMA20 ${data.ema20:,.2f} / EMA50 ${data.ema50:,.2f})",
        f"RSI 14: {data.rsi14:.1f} {rsi_status}",
        f"Funding: {data.funding_percent:.4f}% {funding_status}",
        f"Volume: {data.volume_ratio:.2f}x average {volume_status}",
        "",
        f"<b>Score: {score}/4</b>",
        f"<b>Checklist strength: {score * 25}%</b>",
        "",
        f"<b>Signal: {signal}</b>",
        "",
        f"<i>Bot Uptime: {uptime_str}</i>",
        "",
        "<b>——— HOT COINS ———</b>",
    ]

    if top_gainers:
        lines.append("<b>Top 10 Gainers:</b>")
        for i, (sym, change) in enumerate(top_gainers, 1):
            lines.append(f"{i}. {sym} {change:+.1f}%")
    else:
        lines.append("Top Gainers: unavailable")

    lines.append("")

    if top_volume:
        lines.append("<b>Top 10 Volume:</b>")
        for i, (sym, vol) in enumerate(top_volume, 1):
            vol_m = vol / 1_000_000
            lines.append(f"{i}. {sym} ${vol_m:,.1f}M")
    else:
        lines.append("Top Volume: unavailable")

    if WATCHLIST:
        lines.append("")
        lines.append(f"<b>Your Watchlist:</b> {', '.join(WATCHLIST)}")

    lines += [
        "",
        "<i>Educational alert only — not financial advice.</i>",
        f"<i>Updated: {timestamp.strftime('%Y-%m-%d %H:%M')}</i>",
    ]

    return "\n".join(lines), score, signal


def send_telegram(message: str, timeout: float = 10) -> None:
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    if not token or not chat_id:
        raise ValueError("BOT_TOKEN and CHAT_ID are required.")

    # Telegram has a 4096 character limit
    if len(message) > 4000:
        message = message[:4000] + "\n\n...(truncated)"

    response = requests.post(
        TELEGRAM_URL.format(token=token),
        data={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
        timeout=timeout,
    )
    result = {}
    try:
        result = response.json()
    except ValueError:
        pass

    if not response.ok or not result.get("ok"):
        description = result.get("description", "Telegram rejected the message")
        raise RuntimeError(f"Telegram failed: {description}")
    LOGGER.info("Alert sent to Telegram")


def run_once(*, dry_run: bool = False) -> None:
    global LAST_ALERT_TIME, NEXT_ALERT_TIME

    symbol = os.getenv("SYMBOL", "BTCUSDT").upper()
    interval = os.getenv("CANDLE_INTERVAL", "1h")
    timeout = env_float("REQUEST_TIMEOUT_SECONDS", 15)

    data = get_market_data(symbol=symbol, interval=interval, timeout=timeout)
    message, score, signal = build_message(data)

    if dry_run:
        print(message)
        return

    send_telegram(message)
    LAST_ALERT_TIME = datetime.now().astimezone()
    interval_hours = env_float("RUN_INTERVAL_HOURS", 6)
    NEXT_ALERT_TIME = LAST_ALERT_TIME + timedelta(hours=interval_hours)


def handle_command(text: str) -> str | None:
    global WATCHLIST
    text = text.strip()
    lower = text.lower()

    if lower in ("/start", "start"):
        return (
            "<b>Crypto Alert Bot is online</b>\n\n"
            "Commands:\n"
            "/uptime - Bot uptime\n"
            "/refresh or /now - Send full report now\n"
            "/status - Bot status\n"
            "/watchlist - Show your watchlist\n"
            "/add SYMBOL - Add coin (e.g. /add SOLUSDT)\n"
            "/remove SYMBOL - Remove coin\n"
            "/clearwatchlist - Clear watchlist\n"
            "/help - Show help"
        )

    if lower in ("/help", "help"):
        return (
            "<b>Available Commands</b>\n\n"
            "/uptime\n/refresh or /now\n/status\n"
            "/watchlist\n/add SYMBOL\n/remove SYMBOL\n/clearwatchlist"
        )

    if lower in ("/uptime", "uptime"):
        return f"<b>Bot Uptime:</b> {get_uptime_str()}"

    if lower in ("/refresh", "/now", "refresh", "now"):
        try:
            run_once(dry_run=False)
            return None
        except Exception as e:
            return f"Failed to refresh: {e}"

    if lower in ("/status", "status"):
        uptime = get_uptime_str()
        last = LAST_ALERT_TIME.strftime('%Y-%m-%d %H:%M') if LAST_ALERT_TIME else "Never"
        next_t = NEXT_ALERT_TIME.strftime('%Y-%m-%d %H:%M') if NEXT_ALERT_TIME else "Unknown"
        return (
            f"<b>Bot Status</b>\n\n"
            f"Uptime: {uptime}\n"
            f"Last alert: {last}\n"
            f"Next alert: {next_t}\n"
            f"Watchlist: {', '.join(WATCHLIST) if WATCHLIST else 'Empty'}"
        )

    if lower in ("/watchlist", "watchlist"):
        if not WATCHLIST:
            return "Your watchlist is empty.\nUse /add SYMBOL to add coins."
        return f"<b>Your Watchlist:</b>\n" + "\n".join(f"• {s}" for s in WATCHLIST)

    if lower.startswith("/add ") or lower.startswith("add "):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return "Usage: /add SOLUSDT"
        symbol = parts[1].strip().upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        if symbol in WATCHLIST:
            return f"{symbol} is already in your watchlist."
        WATCHLIST.append(symbol)
        return f"Added <b>{symbol}</b> to watchlist."

    if lower.startswith("/remove ") or lower.startswith("remove "):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return "Usage: /remove SOLUSDT"
        symbol = parts[1].strip().upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        if symbol not in WATCHLIST:
            return f"{symbol} is not in your watchlist."
        WATCHLIST.remove(symbol)
        return f"Removed <b>{symbol}</b> from watchlist."

    if lower in ("/clearwatchlist", "clearwatchlist"):
        WATCHLIST.clear()
        return "Watchlist cleared."

    return None


def telegram_listener():
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    if not token or not chat_id:
        LOGGER.error("BOT_TOKEN or CHAT_ID missing")
        return

    offset = 0
    LOGGER.info("Telegram command listener started")

    while True:
        try:
            response = requests.get(
                TELEGRAM_GET_UPDATES.format(token=token),
                params={"offset": offset, "timeout": 30},
                timeout=35,
            )
            data = response.json()
            if not data.get("ok"):
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if not message:
                    continue
                if str(message["chat"]["id"]) != str(chat_id):
                    continue
                text = message.get("text", "")
                if not text:
                    continue
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
        LOGGER.info("Loaded watchlist from env: %s", WATCHLIST)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    load_watchlist()

    interval_hours = env_float("RUN_INTERVAL_HOURS", 6)
    if interval_hours <= 0:
        raise ValueError("RUN_INTERVAL_HOURS must be > 0")

    if args.dry_run or args.once:
        run_once(dry_run=args.dry_run)
        return

    listener = threading.Thread(target=telegram_listener, daemon=True)
    listener.start()

    LOGGER.info("Bot started | Interval: %.2f hours", interval_hours)

    try:
        run_once(dry_run=False)
    except Exception as e:
        LOGGER.error("Initial alert failed: %s", e)

    while True:
        time.sleep(interval_hours * 3600)
        try:
            run_once(dry_run=False)
        except Exception as e:
            LOGGER.error("Scheduled alert failed: %s", e)


if __name__ == "__main__":
    main()
