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

# Whale settings
WHALE_MIN = 50.0
WHALE_MAX = 3000.0
WHALE_HOURS = 8
WHALE_LIMIT = 6

KNOWN_EXCHANGES = {
    "1NDyJtNTjmwk5xPNhjgAMu4HDHigtobu1s": "Binance",
    "bc1qm34lsc65zpw79lxes69zkqmk6ee3ewf0j77s3h": "Binance",
    "3D2oetdNuZUqQHPJmcMDDHYoqkyNVsFk9r": "Coinbase",
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


def label_address(addr: str) -> str:
    if not addr or addr == "Unknown":
        return "Unknown"

    # First check local known list
    for known, name in KNOWN_EXCHANGES.items():
        if known.lower() in addr.lower():
            return f"{addr} ({name})"

    # Try free satoshidata.ai lookup
    try:
        url = f"https://satoshidata.ai/v1/wallets/{addr}/trust-safety"
        r = requests.get(url, timeout=6)
        if r.status_code == 200:
            data = r.json()
            label = data.get("label", {})
            if isinstance(label, dict):
                value = label.get("value") or label.get("category")
                if value:
                    return f"{addr} ({value})"
    except Exception:
        pass  # silently ignore if the free API fails

    # Fallback heuristics
    if addr.startswith(("1", "3")):
        return f"{addr} (Possible Exchange / Old Wallet)"
    if addr.startswith("bc1q") and len(addr) >= 42:
        return f"{addr} (Possible Exchange)"
    if addr.startswith("bc1p"):
        return f"{addr} (Taproot / Unknown)"

    return f"{addr} (Unknown)"


def is_exchange_like(addr: str) -> bool:
    if not addr or addr == "Unknown":
        return False
    label = label_address(addr).lower()
    return "exchange" in label or "binance" in label or "coinbase" in label


def get_btc_php_rate() -> float:
    try:
        r = requests.get("https://api.exchangerate.host/latest?base=USD&symbols=PHP", timeout=8)
        return float(r.json()["rates"]["PHP"])
    except Exception:
        return 56.5


def get_whale_transactions(btc_price: float) -> tuple[list[dict], float, float]:
    """Returns (whales_list, buy_percent, sell_percent)"""
    global WHALE_MIN, WHALE_MAX, WHALE_HOURS, WHALE_LIMIT
    whales = []
    buy_volume = 0.0
    sell_volume = 0.0

    try:
        blocks = get_json("https://mempool.space/api/v1/blocks", timeout=10)
        if not isinstance(blocks, list):
            return [], 0.0, 0.0

        cutoff = datetime.now(timezone.utc) - timedelta(hours=WHALE_HOURS)
        php_rate = get_btc_php_rate()
        seen = 0

        for block in blocks[:15]:
            if seen >= WHALE_LIMIT:
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

                for tx in txs[:30]:
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

                        # Best-effort Buy / Sell classification
                        from_ex = is_exchange_like(from_addr)
                        to_ex = is_exchange_like(to_addr)

                        if from_ex and not to_ex:
                            buy_volume += amount          # leaving exchange → Buy pressure
                        elif to_ex and not from_ex:
                            sell_volume += amount         # going to exchange → Sell pressure

                        usd = amount * btc_price
                        php = usd * php_rate

                        whales.append({
                            "amount": amount,
                            "usd": usd,
                            "php": php,
                            "from": label_address(from_addr),
                            "to": label_address(to_addr),
                            "time": block_time.strftime("%Y-%m-%d %H:%M UTC"),
                        })
                        seen += 1
                        if seen >= WHALE_LIMIT:
                            break
                    except Exception:
                        continue
            except Exception:
                continue

        total = buy_volume + sell_volume
        if total > 0:
            buy_pct = (buy_volume / total) * 100
            sell_pct = (sell_volume / total) * 100
        else:
            buy_pct = sell_pct = 0.0

        return whales, buy_pct, sell_pct

    except Exception as e:
        LOGGER.warning("Whale data unavailable: %s", e)
        return [], 0.0, 0.0


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
    d = up.days
    h, rem = divmod(up.seconds, 3600)
    m = rem // 60
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

    v = "Strong" if data.volume_ratio >= 1.5 else "Supporting" if data.volume_ratio >= 1 else "Weak"
    lines.append(f"• Volume: {v}")

    pts = sum([data.ema20 > data.ema50, data.change_24h > 1, 45 <= data.rsi14 <= 65, data.volume_ratio >= 1])
    bias = "Cautiously bullish" if pts >= 3 else "Cautiously bearish/weak" if pts <= 1 else "Mixed/neutral"
    lines.append(f"• Overall: {bias}")
    lines.append("<i>Technical data only — not financial advice.</i>")
    return lines


def build_message(data: MarketData) -> str:
    ts = datetime.now().astimezone()
    score, signal = score_market(data)
    uptime = get_uptime_str()

    # Funding status
    if data.funding_percent < 0:
        fund_status = "GOOD (short bias)"
    elif data.funding_percent > 0.05:
        fund_status = "HIGH (long bias)"
    else:
        fund_status = "NEUTRAL"

    lines = [
        f"<b>PRO MARKET CHECKLIST — {ts.strftime('%b %d %I:%M %p')}</b>",
        "",
        f"<b>{data.symbol}</b>: ${data.price:,.2f} ({data.change_24h:+.2f}% 24h)",
        f"Trend: {'BULLISH' if data.ema20 > data.ema50 else 'BEARISH'} | RSI {data.rsi14:.1f}",
        f"Funding Rate: <b>{data.funding_percent:.4f}%</b> ({fund_status})",
        f"Volume: {data.volume_ratio:.2f}x",
        "",
        f"<b>Score: {score}/4</b> → <b>{signal}</b>",
        "",
    ]
    lines.extend(analyze_trend(data))
    lines.append("")

    # Whale section
    lines.append(f"<b>🐋 WHALE ALERT ({WHALE_MIN:.0f}–{WHALE_MAX:.0f} BTC | Last {WHALE_HOURS}h)</b>")
    whales, buy_pct, sell_pct = get_whale_transactions(data.price)

    if buy_pct or sell_pct:
        dominant = "BUY" if buy_pct > sell_pct else "SELL" if sell_pct > buy_pct else "BALANCED"
        lines.append(f"Buy vs Sell: <b>{buy_pct:.1f}% Buy</b> / <b>{sell_pct:.1f}% Sell</b> → {dominant}")
        lines.append("")

    if whales:
        for w in whales:
            lines.append(
                f"• <b>{w['amount']:.2f} BTC</b> ≈ ${w['usd']:,.0f} ≈ ₱{w['php']:,.0f}\n"
                f"  From: <code>{w['from']}</code>\n"
                f"  To:   <code>{w['to']}</code>\n"
                f"  Time: {w['time']}"
            )
    else:
        lines.append("No matching whales found in the selected time window")

    if WATCHLIST:
        lines.append("")
        lines.append(f"<b>Watchlist:</b> {', '.join(WATCHLIST)}")

    lines += [
        "",
        f"<i>Uptime: {uptime}</i>",
        f"<i>Whale settings: {WHALE_MIN:.0f}-{WHALE_MAX:.0f} BTC | {WHALE_HOURS}h | limit {WHALE_LIMIT}</i>",
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
    global WHALE_MIN, WHALE_MAX, WHALE_HOURS, WHALE_LIMIT, WATCHLIST, CHAT_IDS
    lower = text.strip().lower()
    is_admin = bool(CHAT_IDS) and from_id == CHAT_IDS[0]

    if lower in ("/start", "start", "/help"):
        return (
            "<b>Crypto Alert Bot — Commands</b>\n\n"
            "/uptime\n/refresh or /now\n/status\n"
            "/whalesettings\n"
            "/whalemin 50\n/whalemax 3000\n"
            "/whalehours 8\n/whalelimit 10\n"
            "/watchlist\n/add SYMBOL\n/remove SYMBOL\n"
            "/subscribers (admin)\n/adduser ID (admin)\n/removeuser ID (admin)"
        )

    if lower == "/uptime":
        return f"Uptime: <b>{get_uptime_str()}</b>"

    if lower in ("/refresh", "/now"):
        try:
            run_once()
            return "Full report sent."
        except Exception as e:
            return f"Error: {e}"

    if lower == "/status":
        last = LAST_ALERT_TIME.strftime("%Y-%m-%d %H:%M") if LAST_ALERT_TIME else "—"
        return (
            f"Uptime: {get_uptime_str()}\nLast: {last}\n"
            f"Whale: {WHALE_MIN:.0f}-{WHALE_MAX:.0f} BTC | {WHALE_HOURS}h | limit {WHALE_LIMIT}\n"
            f"Subscribers: {len(CHAT_IDS)}"
        )

    if lower == "/whalesettings":
        return (
            f"<b>Whale Settings</b>\n"
            f"Min: {WHALE_MIN:.0f} BTC\nMax: {WHALE_MAX:.0f} BTC\n"
            f"Time window: Last {WHALE_HOURS} hours\n"
            f"Show limit: {WHALE_LIMIT}"
        )

    if lower.startswith("/whalemin "):
        try:
            val = float(text.split()[1])
            if val < 1:
                return "Min must be ≥ 1"
            WHALE_MIN = val
            return f"Min set to <b>{val:.0f} BTC</b>"
        except Exception:
            return "Usage: /whalemin 50"

    if lower.startswith("/whalemax "):
        try:
            val = float(text.split()[1])
            if val <= WHALE_MIN:
                return "Max must be higher than min"
            WHALE_MAX = val
            return f"Max set to <b>{val:.0f} BTC</b>"
        except Exception:
            return "Usage: /whalemax 3000"

    if lower.startswith("/whalehours "):
        try:
            val = int(text.split()[1])
            if not 1 <= val <= 48:
                return "Choose 1–48 hours"
            WHALE_HOURS = val
            return f"Time window set to <b>{val} hours</b>"
        except Exception:
            return "Usage: /whalehours 8"

    if lower.startswith("/whalelimit "):
        try:
            val = int(text.split()[1])
            if not 1 <= val <= 20:
                return "Choose 1–20"
            WHALE_LIMIT = val
            return f"Limit set to <b>{val}</b>"
        except Exception:
            return "Usage: /whalelimit 10"

    if lower == "/watchlist":
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

    if lower == "/subscribers" and is_admin:
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
            LOGGER.warning("Listener error: %s", e)
            time.sleep(10)


def load_config():
    global CHAT_IDS, WATCHLIST, WHALE_MIN, WHALE_MAX, WHALE_HOURS, WHALE_LIMIT
    CHAT_IDS = [x.strip() for x in os.getenv("CHAT_ID", "").split(",") if x.strip()]
    WATCHLIST = [s.strip().upper() for s in os.getenv("SYMBOLS", "").split(",") if s.strip()]
    WHALE_MIN = env_float("WHALE_MIN", 50)
    WHALE_MAX = env_float("WHALE_MAX", 3000)
    WHALE_HOURS = int(env_float("WHALE_HOURS", 8))
    WHALE_LIMIT = int(env_float("WHALE_LIMIT", 6))


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
    LOGGER.info("Bot started | every %.1fh | subs: %d", hours, len(CHAT_IDS))

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
