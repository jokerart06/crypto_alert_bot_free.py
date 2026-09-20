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

# Whale settings (can be changed via commands)
WHALE_MIN = 50.0
WHALE_MAX = 3000.0

# Best-effort known exchange addresses (short list)
KNOWN_EXCHANGES = {
    # Binance examples (these change often)
    "1NDyJtNTjmwk5xPNhjgAMu4HDHigtobu1s": "Binance",
    "3KZ3y5Qn6qYw7xZ8v9pR2tU4sW6xY8zA1b": "Binance",
    # Add more known ones if needed
}


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
        raise ValueError(f"Need at least {period} values")
    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * multiplier + ema
    return ema


def calculate_rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        raise ValueError(f"Need at least {period+1} values")
    gains, losses = [], []
    for prev, curr in zip(values, values[1:]):
        change = curr - prev
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
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
        funding = 0.0
        try:
            fp = get_json(FUNDING_URL, {"symbol": symbol}, timeout)
            if isinstance(fp, dict):
                funding = float(fp.get("lastFundingRate", 0)) * 100
        except Exception as e:
            LOGGER.warning("Funding unavailable: %s", e)

        ticker = get_json(TICKER_URL, {"symbol": symbol}, timeout)
        klines = get_json(KLINES_URL, {"symbol": symbol, "interval": interval, "limit": 100}, timeout)

        if not isinstance(ticker, dict):
            raise MarketDataError("Invalid ticker")

        closes, volumes = _parse_klines(klines)
        if len(volumes) < 21:
            raise MarketDataError("Not enough candles")

        avg = sum(volumes[-21:-1]) / 20
        vol_ratio = volumes[-1] / avg if avg > 0 else 0.0

        return MarketData(
            symbol=symbol,
            interval=interval,
            price=float(ticker["lastPrice"]),
            change_24h=float(ticker["priceChangePercent"]),
            funding_percent=funding,
            ema20=calculate_ema(closes, 20),
            ema50=calculate_ema(closes, 50),
            rsi14=calculate_rsi(closes, 14),
            volume_ratio=vol_ratio,
        )
    except MarketDataError:
        raise
    except Exception as e:
        raise MarketDataError(str(e)) from e


def get_hot_coins(timeout: float = 12) -> tuple[list, list]:
    try:
        data = get_json(TICKER_URL, timeout=timeout)
        usdt = [
            t for t in data
            if isinstance(t, dict)
            and str(t.get("symbol", "")).endswith("USDT")
            and not any(x in t["symbol"] for x in ("UPUSDT", "DOWNUSDT", "BULL", "BEAR"))
        ]
        gainers = sorted(usdt, key=lambda x: float(x.get("priceChangePercent", 0)), reverse=True)[:8]
        top_g = [(t["symbol"], float(t["priceChangePercent"])) for t in gainers]
        by_vol = sorted(usdt, key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)[:6]
        top_v = [(t["symbol"], float(t["quoteVolume"])) for t in by_vol]
        return top_g, top_v
    except Exception as e:
        LOGGER.warning("Hot coins error: %s", e)
        return [], []


def label_address(addr: str) -> str:
    """Best-effort exchange label."""
    if not addr or addr == "Unknown":
        return "Unknown"
    for known, name in KNOWN_EXCHANGES.items():
        if known in addr:
            return f"{addr} ({name})"
    # Simple heuristics
    if addr.startswith("bc1q") and len(addr) > 40:
        return f"{addr} (Possible Exchange)"
    return addr


def get_btc_php_rate() -> float:
    """Approximate PHP rate. Falls back to a safe default."""
    try:
        # Free endpoint for USD/PHP
        r = requests.get("https://api.exchangerate.host/latest?base=USD&symbols=PHP", timeout=8)
        data = r.json()
        return float(data["rates"]["PHP"])
    except Exception:
        return 56.5  # fallback approximate rate


def get_whale_transactions(btc_price: float, hours: int = 8) -> list[dict]:
    global WHALE_MIN, WHALE_MAX
    whales = []
    try:
        blocks = get_json("https://mempool.space/api/v1/blocks", timeout=10)
        if not isinstance(blocks, list):
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        php_rate = get_btc_php_rate()
        seen = 0

        for block in blocks[:10]:
            if seen >= 6:
                break
            try:
                block_time = datetime.fromtimestamp(block["timestamp"], tz=timezone.utc)
                if block_time < cutoff:
                    continue

                block_hash = block.get("id")
                if not block_hash:
                    continue

                txs = get_json(f"https://mempool.space/api/block/{block_hash}/txs", timeout=10)
                if not isinstance(txs, list):
                    continue

                for tx in txs[:25]:
                    try:
                        total_out = sum(v.get("value", 0) for v in tx.get("vout", []))
                        amount = total_out / 1e8

                        if not (WHALE_MIN <= amount <= WHALE_MAX):
                            continue

                        vin = tx.get("vin", [])
                        vout = tx.get("vout", [])

                        from_addr = "Unknown"
                        to_addr = "Unknown"
                        if vin and vin[0].get("prevout"):
                            from_addr = vin[0]["prevout"].get("scriptpubkey_address", "Unknown")
                        if vout:
                            to_addr = vout[0].get("scriptpubkey_address", "Unknown")

                        usd = amount * btc_price
                        php = usd * php_rate

                        whales.append({
                            "amount": amount,
                            "usd": usd,
                            "php": php,
                            "from": label_address(from_addr),
                            "to": label_address(to_addr),
                            "time": block_time.strftime("%H:%M UTC"),
                        })
                        seen += 1
                        if seen >= 6:
                            break
                    except Exception:
                        continue
            except Exception:
                continue
        return whales
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
    up = datetime.now().astimezone() - START_TIME
    d, rem = up.days, up.seconds
    h, m = divmod(rem, 3600)[0], divmod(rem, 3600)[1] // 60
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"


def analyze_trend(data: MarketData) -> list[str]:
    lines = ["<b>📊 TREND ANALYZER</b>"]
    diff = ((data.ema20 - data.ema50) / data.ema50 * 100) if data.ema50 else 0
    if data.ema20 > data.ema50:
        t = "Strong bullish" if diff > 1.5 else "Bullish" if diff > 0.4 else "Mild bullish"
    else:
        t = "Strong bearish" if diff < -1.5 else "Bearish" if diff < -0.4 else "Mild bearish"
    lines.append(f"• Structure: {t}")

    if data.change_24h > 5:
        m = "Strong positive"
    elif data.change_24h > 1.5:
        m = "Positive"
    elif data.change_24h > -1.5:
        m = "Neutral"
    else:
        m = "Negative"
    lines.append(f"• 24h Momentum: {m} ({data.change_24h:+.2f}%)")

    if data.rsi14 >= 70:
        r = "Overbought"
    elif data.rsi14 >= 60:
        r = "Upper neutral"
    elif data.rsi14 >= 45:
        r = "Healthy"
    elif data.rsi14 >= 30:
        r = "Lower neutral"
    else:
        r = "Oversold"
    lines.append(f"• RSI: {r} ({data.rsi14:.1f})")

    v = "Strong confirmation" if data.volume_ratio >= 1.5 else "Supporting" if data.volume_ratio >= 1 else "Weak"
    lines.append(f"• Volume: {v}")

    pts = sum([
        data.ema20 > data.ema50,
        data.change_24h > 1,
        45 <= data.rsi14 <= 65,
        data.volume_ratio >= 1
    ])
    bias = "Cautiously bullish" if pts >= 3 else "Cautiously bearish/weak" if pts <= 1 else "Mixed/neutral"
    lines.append(f"• Overall: {bias}")
    lines.append("<i>Technical data only — not financial advice.</i>")
    return lines


def build_message(data: MarketData) -> str:
    global WHALE_MIN, WHALE_MAX
    ts = datetime.now().astimezone()
    score, signal = score_market(data)
    uptime = get_uptime_str()

    lines = [
        f"<b>PRO MARKET CHECKLIST — {ts.strftime('%b %d %I:%M %p')}</b>",
        "",
        f"<b>{data.symbol}</b>: ${data.price:,.2f} ({data.change_24h:+.2f}% 24h)",
        f"Trend: {'BULLISH' if data.ema20 > data.ema50 else 'BEARISH'} | RSI {data.rsi14:.1f} | Vol {data.volume_ratio:.2f}x",
        f"Score: <b>{score}/4</b> → <b>{signal}</b>",
        "",
    ]
    lines.extend(analyze_trend(data))
    lines.append("")

    # Hot coins
    gainers, vols = get_hot_coins()
    lines.append("<b>🔥 HOT COINS</b>")
    if gainers:
        lines.append("Top Gainers:")
        for i, (s, c) in enumerate(gainers[:6], 1):
            lines.append(f"{i}. {s} {c:+.1f}%")
    if vols:
        lines.append("Top Volume:")
        for i, (s, v) in enumerate(vols[:4], 1):
            lines.append(f"{i}. {s} ${v/1e6:,.0f}M")

    # Whale section
    lines.append("")
    lines.append(f"<b>🐋 WHALE ALERT ({WHALE_MIN:.0f}–{WHALE_MAX:.0f} BTC)</b>")
    whales = get_whale_transactions(data.price, hours=8)
    if whales:
        for w in whales:
            lines.append(
                f"• <b>{w['amount']:.2f} BTC</b> ≈ ${w['usd']:,.0f} ≈ ₱{w['php']:,.0f}\n"
                f"  From: <code>{w['from']}</code>\n"
                f"  To:   <code>{w['to']}</code>\n"
                f"  Time: {w['time']}"
            )
    else:
        lines.append("No matching whales in last 8h (or data unavailable)")

    if WATCHLIST:
        lines.append("")
        lines.append(f"<b>Watchlist:</b> {', '.join(WATCHLIST)}")

    lines += [
        "",
        f"<i>Uptime: {uptime} | Min/Max Whale: {WHALE_MIN:.0f}/{WHALE_MAX:.0f} BTC</i>",
        "<i>Educational only — not financial advice.</i>",
    ]
    return "\n".join(lines)


def send_telegram(message: str, chat_id: str | None = None) -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise ValueError("BOT_TOKEN missing")

    targets = [chat_id] if chat_id else CHAT_IDS
    if len(message) > 4090:
        message = message[:4000] + "\n...(truncated)"

    for cid in targets:
        try:
            r = requests.post(
                TELEGRAM_URL.format(token=token),
                data={"chat_id": cid, "text": message, "parse_mode": "HTML"},
                timeout=12,
            )
            if r.ok:
                LOGGER.info("Sent to %s", cid)
            else:
                LOGGER.warning("Failed %s: %s", cid, r.text[:150])
        except Exception as e:
            LOGGER.warning("Send error %s: %s", cid, e)


def run_once(dry_run: bool = False) -> None:
    global LAST_ALERT_TIME, NEXT_ALERT_TIME
    symbol = os.getenv("SYMBOL", "BTCUSDT").upper()
    interval = os.getenv("CANDLE_INTERVAL", "1h")
    data = get_market_data(symbol, interval, env_float("REQUEST_TIMEOUT_SECONDS", 15))
    msg = build_message(data)

    if dry_run:
        print(msg)
        return

    send_telegram(msg)
    LAST_ALERT_TIME = datetime.now().astimezone()
    NEXT_ALERT_TIME = LAST_ALERT_TIME + timedelta(hours=env_float("RUN_INTERVAL_HOURS", 6))


def handle_command(text: str, from_id: str) -> str | None:
    global WHALE_MIN, WHALE_MAX, WATCHLIST, CHAT_IDS
    lower = text.strip().lower()
    is_admin = bool(CHAT_IDS) and from_id == CHAT_IDS[0]

    if lower in ("/start", "start"):
        return (
            "<b>Crypto Alert Bot</b>\n\n"
            "/uptime /refresh /status\n"
            "/whalesettings /whalemin X /whalemax X\n"
            "/watchlist /add SYMBOL /remove SYMBOL\n"
            "/subscribers /adduser ID /removeuser ID (admin)"
        )

    if lower in ("/uptime",):
        return f"Uptime: <b>{get_uptime_str()}</b>"

    if lower in ("/refresh", "/now"):
        try:
            run_once()
            return "Report sent."
        except Exception as e:
            return f"Error: {e}"

    if lower in ("/status",):
        last = LAST_ALERT_TIME.strftime("%H:%M") if LAST_ALERT_TIME else "—"
        return f"Uptime: {get_uptime_str()}\nLast: {last}\nWhale: {WHALE_MIN:.0f}–{WHALE_MAX:.0f} BTC\nSubs: {len(CHAT_IDS)}"

    if lower in ("/whalesettings",):
        return f"Whale filter: <b>{WHALE_MIN:.0f} – {WHALE_MAX:.0f} BTC</b>"

    if lower.startswith("/whalemin "):
        try:
            val = float(text.split()[1])
            if val < 1:
                return "Minimum must be ≥ 1"
            WHALE_MIN = val
            return f"Whale minimum set to <b>{val:.0f} BTC</b>"
        except Exception:
            return "Usage: /whalemin 50"

    if lower.startswith("/whalemax "):
        try:
            val = float(text.split()[1])
            if val <= WHALE_MIN:
                return "Maximum must be higher than minimum"
            WHALE_MAX = val
            return f"Whale maximum set to <b>{val:.0f} BTC</b>"
        except Exception:
            return "Usage: /whalemax 3000"

    if lower in ("/watchlist",):
        return "Watchlist:\n" + ("\n".join(f"• {s}" for s in WATCHLIST) or "Empty")

    if lower.startswith("/add "):
        sym = text.split(maxsplit=1)[1].strip().upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        if sym not in WATCHLIST:
            WATCHLIST.append(sym)
        return f"Added {sym}"

    if lower.startswith("/remove "):
        sym = text.split(maxsplit=1)[1].strip().upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        if sym in WATCHLIST:
            WATCHLIST.remove(sym)
            return f"Removed {sym}"
        return "Not found"

    # Admin
    if lower in ("/subscribers",) and is_admin:
        return "Subscribers:\n" + "\n".join(CHAT_IDS)

    if lower.startswith("/adduser ") and is_admin:
        uid = text.split(maxsplit=1)[1].strip()
        if uid not in CHAT_IDS:
            CHAT_IDS.append(uid)
            return f"Added {uid}"
        return "Already exists"

    if lower.startswith("/removeuser ") and is_admin:
        uid = text.split(maxsplit=1)[1].strip()
        if uid in CHAT_IDS and uid != CHAT_IDS[0]:
            CHAT_IDS.remove(uid)
            return f"Removed {uid}"
        return "Cannot remove"

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
                cid = str(msg["chat"]["id"])
                text = msg.get("text", "")
                if text:
                    reply = handle_command(text, cid)
                    if reply:
                        send_telegram(reply, chat_id=cid)
        except Exception as e:
            LOGGER.warning("Listener: %s", e)
            time.sleep(10)


def load_config():
    global CHAT_IDS, WATCHLIST, WHALE_MIN, WHALE_MAX
    CHAT_IDS = [x.strip() for x in os.getenv("CHAT_ID", "").split(",") if x.strip()]
    WATCHLIST = [s.strip().upper() for s in os.getenv("SYMBOLS", "").split(",") if s.strip()]
    WHALE_MIN = env_float("WHALE_MIN", 50)
    WHALE_MAX = env_float("WHALE_MAX", 3000)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
    load_config()

    if not CHAT_IDS:
        raise ValueError("CHAT_ID required")

    if args.dry_run or args.once:
        run_once(dry_run=args.dry_run)
        return

    threading.Thread(target=telegram_listener, daemon=True).start()
    hours = env_float("RUN_INTERVAL_HOURS", 6)
    LOGGER.info("Bot started | every %.1fh | subs: %d | whale: %.0f-%.0f", hours, len(CHAT_IDS), WHALE_MIN, WHALE_MAX)

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
