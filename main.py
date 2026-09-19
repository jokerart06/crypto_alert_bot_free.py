from __future__ import annotations

import argparse
import csv
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

LOGGER = logging.getLogger("crypto_alert_bot_free")

FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr"
KLINES_URL = "https://api.binance.com/api/v3/klines"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


class MarketDataError(RuntimeError):
    """Raised when the bot cannot safely calculate a market signal."""


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


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def get_json(url: str, params: dict[str, str | int], timeout: float) -> Any:
    response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def calculate_ema(values: list[float], period: int) -> float:
    """Calculate the latest exponential moving average."""
    if len(values) < period:
        raise ValueError(f"At least {period} values are required for EMA")

    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period

    for value in values[period:]:
        ema = (value - ema) * multiplier + ema

    return ema


def calculate_rsi(values: list[float], period: int = 14) -> float:
    """Calculate the latest Wilder RSI value."""
    if len(values) < period + 1:
        raise ValueError(f"At least {period + 1} values are required for RSI")

    gains: list[float] = []
    losses: list[float] = []

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

    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def _parse_klines(payload: Any) -> tuple[list[float], list[float]]:
    if not isinstance(payload, list):
        raise MarketDataError("Binance returned invalid candle data")

    closes: list[float] = []
    volumes: list[float] = []

    for candle in payload:
        if not isinstance(candle, list) or len(candle) < 6:
            raise MarketDataError("Binance returned an incomplete candle")
        closes.append(float(candle[4]))
        volumes.append(float(candle[5]))

    return closes, volumes


def get_market_data(symbol: str, interval: str, timeout: float) -> MarketData:
    """Fetch live Binance data and calculate indicators."""
    try:
        funding_payload = get_json(FUNDING_URL, params={"symbol": symbol}, timeout=timeout)
        ticker_payload = get_json(TICKER_URL, params={"symbol": symbol}, timeout=timeout)
        klines_payload = get_json(
            KLINES_URL,
            params={"symbol": symbol, "interval": interval, "limit": 100},
            timeout=timeout,
        )

        if not isinstance(funding_payload, dict):
            raise MarketDataError("Binance returned invalid funding data")
        if not isinstance(ticker_payload, dict):
            raise MarketDataError("Binance returned invalid ticker data")

        closes, volumes = _parse_klines(klines_payload)

        if len(volumes) < 21:
            raise MarketDataError("Not enough candles for volume analysis")

        average_previous_volume = sum(volumes[-21:-1]) / 20
        volume_ratio = (
            volumes[-1] / average_previous_volume if average_previous_volume > 0 else 0.0
        )

        return MarketData(
            symbol=symbol,
            interval=interval,
            price=float(ticker_payload["lastPrice"]),
            change_24h=float(ticker_payload["priceChangePercent"]),
            funding_percent=float(funding_payload.get("lastFundingRate", 0)) * 100,
            ema20=calculate_ema(closes, 20),
            ema50=calculate_ema(closes, 50),
            rsi14=calculate_rsi(closes, 14),
            volume_ratio=volume_ratio,
        )

    except MarketDataError:
        raise
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        raise MarketDataError(f"Could not fetch market data: {exc}") from exc


def score_market(data: MarketData) -> tuple[int, str]:
    """Return checklist score and signal label."""
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


def build_message(data: MarketData, now: datetime | None = None) -> tuple[str, int, str]:
    """Build Telegram message, score, and signal label."""
    timestamp = now or datetime.now().astimezone()
    score, signal = score_market(data)

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

    lines = [
        f"<b> PRO MARKET CHECKLIST - {timestamp.strftime('%b %d %I:%M %p')}</b>",
        "",
        f"<b>{data.symbol}</b>: ${data.price:,.2f} ({change_sign}{data.change_24h:.2f}% 24h)",
        "",
        f"Trend: {trend_status}(EMA20 ${data.ema20:,.2f} / EMA50 ${data.ema50:,.2f})",
        f"RSI 14: {data.rsi14:.1f} {rsi_status}",
        f"Funding: {data.funding_percent:.4f}% {funding_status}",
        f"Volume: {data.volume_ratio:.2f}x average {volume_status}",
        "",
        f"<b>Score: {score}/4</b>",
        f"<b>Checklist strength: {score * 25}%</b> (not a probability)",
        "",
        f"<b>Signal: {signal}</b>",
        "",
        "<i>Educational alert only — not financial advice. Confirm before trading.</i>",
        f"<i>Updated: {timestamp.strftime('%Y-%m-%d %H:%M')}</i>",
    ]

    return "\n".join(lines), score, signal


def append_signal_history(data: MarketData, score: int, signal: str) -> None:
    """Save every real alert for later evaluation."""
    history_path = Path(os.getenv("SIGNAL_HISTORY_FILE", "signal_history.csv"))

    fieldnames = [
        "timestamp",
        "symbol",
        "interval",
        "price",
        "change_24h",
        "funding_percent",
        "ema20",
        "ema50",
        "rsi14",
        "volume_ratio",
        "score",
        "signal",
    ]

    file_exists = history_path.exists()

    try:
        with history_path.open("a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()

            writer.writerow(
                {
                    "timestamp": datetime.now().astimezone().isoformat(),
                    "symbol": data.symbol,
                    "interval": data.interval,
                    "price": f"{data.price:.8f}",
                    "change_24h": f"{data.change_24h:.4f}",
                    "funding_percent": f"{data.funding_percent:.6f}",
                    "ema20": f"{data.ema20:.8f}",
                    "ema50": f"{data.ema50:.8f}",
                    "rsi14": f"{data.rsi14:.4f}",
                    "volume_ratio": f"{data.volume_ratio:.4f}",
                    "score": score,
                    "signal": signal,
                }
            )
    except OSError as exc:
        LOGGER.warning("Could not save signal history: %s", exc)


def send_telegram(message: str, timeout: float = 10) -> None:
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")

    if not token or not chat_id:
        raise ValueError(
            "BOT_TOKEN and CHAT_ID are required. Use --dry-run to preview instead."
        )

    response = requests.post(
        TELEGRAM_URL.format(token=token),
        data={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        },
        timeout=timeout,
    )

    try:
        result = response.json()
    except ValueError:
        result = {}

    if not response.ok or not result.get("ok"):
        description = result.get("description", "Telegram rejected the message")
        raise RuntimeError(
            f"Telegram request failed (HTTP {response.status_code}): {description}"
        )

    LOGGER.info("Alert sent to Telegram")


def run_once(*, dry_run: bool) -> None:
    symbol = os.getenv("SYMBOL", "BTCUSDT").upper()
    interval = os.getenv("CANDLE_INTERVAL", "1h")
    timeout = env_float("REQUEST_TIMEOUT_SECONDS", 15)

    data = get_market_data(symbol=symbol, interval=interval, timeout=timeout)
    message, score, signal = build_message(data)

    if dry_run:
        print(message)
        return

    send_telegram(message, timeout=timeout)
    append_signal_history(data, score, signal)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the free Bitcoin alert bot.")
    parser.add_argument("--once", action="store_true", help="send one alert and exit")
    parser.add_argument("--dry-run", action="store_true", help="print the alert without sending it")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    interval_hours = env_float("RUN_INTERVAL_HOURS", 6)
    if interval_hours <= 0:
        raise ValueError("RUN_INTERVAL_HOURS must be greater than zero")

    if args.dry_run or args.once:
        run_once(dry_run=args.dry_run)
        return

    LOGGER.info("Bot started; sending every %.2f hours", interval_hours)

    while True:
        try:
            run_once(dry_run=False)
        except MarketDataError as exc:
            LOGGER.error("Alert skipped because market data was unsafe: %s", exc)
        except Exception:
            LOGGER.exception("Alert cycle failed")

        time.sleep(interval_hours * 60 * 60)


if __name__ == "__main__":
    main()
