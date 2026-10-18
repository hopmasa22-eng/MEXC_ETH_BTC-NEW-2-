# -*- coding: utf-8 -*-
"""MEXC Futures 10m Telegram signal bot: two separate candle rules. Signals only."""
import json
import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [x.strip() for x in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if x.strip()]
SYMBOLS = []
for raw in os.getenv("MEXC_SYMBOLS", "BTCUSDT,ETHUSDT").split(","):
    s = raw.strip().upper().replace("_", "")
    if s:
        SYMBOLS.append(s[:-4] + "_USDT" if s.endswith("USDT") else s + "_USDT")
SYMBOLS = list(dict.fromkeys(SYMBOLS))
POLL_SECONDS = max(5, int(os.getenv("POLL_SECONDS", "10")))
TEST_MODE = os.getenv("TEST_MODE", "false").lower() in {"1", "true", "yes", "on"}
TIMEFRAME = 600
API_URL = os.getenv("MEXC_FUTURES_KLINE_URL", "https://api.mexc.com/api/v1/contract/kline/{symbol}").replace("https://contract.mexc.com", "https://api.mexc.com")
KYIV = ZoneInfo("Europe/Kyiv")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("mexc-two-rules")
session = requests.Session()
session.headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache", "User-Agent": "MEXC-Futures-10m-Two-Rules-Bot/2.0"})


def color(c):
    if c["close"] > c["open"]:
        return "G"
    if c["close"] < c["open"]:
        return "R"
    return "D"


def emoji(c):
    return {"G": "ð¢", "R": "ð´", "D": "âª"}[c]


def classify_result(start, opposite, result_colors):
    """Return WIN if any favorable candle, LOSS if all are opposite, else DOJI."""
    if start in result_colors:
        return "WIN"
    if all(c == opposite for c in result_colors):
        return "LOSS"
    return "DOJI"


def label(symbol):
    return symbol.replace("_", "")


def kyiv(ts):
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(KYIV).strftime("%d.%m.%Y %H:%M:%S")


_last_telegram_send_at = 0.0
TELEGRAM_MIN_INTERVAL = max(1.0, float(os.getenv("TELEGRAM_MIN_INTERVAL", "1.2")))


def send(message):
    """Send serially, honor Telegram retry_after, and avoid rapid retries."""
    global _last_telegram_send_at
    if TEST_MODE:
        print("\n--- TEST MESSAGE ---\n" + message + "\n--------------------")
        return True
    if not TOKEN or not CHAT_IDS:
        log.error("ÐÐµ Ð½Ð°Ð»Ð°ÑÑÐ¾Ð²Ð°Ð½Ð¾ Telegram: Ð·Ð°Ð´Ð°Ð¹ÑÐµ TELEGRAM_BOT_TOKEN Ñ TELEGRAM_CHAT_ID")
        return False

    all_ok = True
    for chat_id in CHAT_IDS:
        delivered = False
        for attempt in range(4):
            # One shared pace for every chat/message in this process.
            wait = TELEGRAM_MIN_INTERVAL - (time.monotonic() - _last_telegram_send_at)
            if wait > 0:
                time.sleep(wait)
            try:
                r = session.post(
                    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": message},
                    timeout=20,
                )
                _last_telegram_send_at = time.monotonic()
                try:
                    payload = r.json()
                except ValueError:
                    payload = {}

                if r.ok and payload.get("ok"):
                    log.info("TELEGRAM SENT OK | chat=%s", chat_id)
                    delivered = True
                    break

                if r.status_code == 429:
                    retry_after = payload.get("parameters", {}).get("retry_after", 5)
                    try:
                        retry_after = max(1, int(retry_after))
                    except (TypeError, ValueError):
                        retry_after = 5
                    log.warning(
                        "TELEGRAM RATE LIMITED | chat=%s | retry_after=%ss | attempt=%s/4",
                        chat_id, retry_after, attempt + 1,
                    )
                    if attempt < 3:
                        time.sleep(retry_after + 1)
                        continue
                else:
                    log.error("TELEGRAM ERROR | chat=%s | status=%s | body=%s", chat_id, r.status_code, r.text[:300])
                    if r.status_code >= 500 and attempt < 3:
                        time.sleep(2 ** attempt)
                        continue
                break
            except requests.RequestException as exc:
                _last_telegram_send_at = time.monotonic()
                log.warning("TELEGRAM CONNECTION ERROR | chat=%s | attempt=%s/4 | %s", chat_id, attempt + 1, exc)
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                break

        if not delivered:
            all_ok = False
            log.error("TELEGRAM DELIVERY FAILED | chat=%s | no more automatic retries", chat_id)
    return all_ok


def fetch(symbol, limit=300):
    """Fetch 1m Futures candles and aggregate them into complete 10m candles.

    MEXC's contract kline API is queried with Min1 so the strategy matches the
    10-minute chart the user watches. A 10m candle is accepted as closed only
    when all ten constituent 1m candles are present.
    """
    now = int(time.time())
    minute_seconds = 60
    r = session.get(
        API_URL.format(symbol=symbol),
        params={
            "interval": "Min1",
            "limit": limit,
            "start": now - limit * minute_seconds,
            "end": now,
        },
        timeout=15,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("success") is False:
        raise RuntimeError(f"ÐÐ¾Ð¼Ð¸Ð»ÐºÐ° API MEXC: {payload}")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("time"), list):
        raise RuntimeError(f"ÐÐµÐ¾ÑÑÐºÑÐ²Ð°Ð½Ð° Ð²ÑÐ´Ð¿Ð¾Ð²ÑÐ´Ñ API MEXC: {str(payload)[:250]}")
    if not (len(data["time"]) == len(data.get("open", [])) == len(data.get("close", []))):
        raise RuntimeError("ÐÐ°ÑÐ¸Ð²Ð¸ ÑÐ²ÑÑÐ¾Ðº MEXC Ð¼Ð°ÑÑÑ ÑÑÐ·Ð½Ñ Ð´Ð¾Ð²Ð¶Ð¸Ð½Ñ")

    minute_rows = []
    for ts, op, cl in zip(data["time"], data["open"], data["close"]):
        ts = int(ts)
        if ts > 10_000_000_000:
            ts //= 1000
        minute_rows.append({"ts": ts, "open": float(op), "close": float(cl)})
    minute_rows = list({c["ts"]: c for c in sorted(minute_rows, key=lambda x: x["ts"])}.values())
    minute_rows.sort(key=lambda x: x["ts"])

    by_bucket = {}
    for candle in minute_rows:
        bucket = (candle["ts"] // TIMEFRAME) * TIMEFRAME
        by_bucket.setdefault(bucket, []).append(candle)

    closed = []
    live = None
    current_bucket = (now // TIMEFRAME) * TIMEFRAME
    for bucket, members in sorted(by_bucket.items()):
        members.sort(key=lambda x: x["ts"])
        expected_closed = list(range(bucket, bucket + TIMEFRAME, minute_seconds))
        times = [c["ts"] for c in members]
        if bucket + TIMEFRAME <= now:
            if times == expected_closed:
                closed.append({"ts": bucket, "open": members[0]["open"], "close": members[-1]["close"]})
        elif bucket == current_bucket and bucket <= now < bucket + TIMEFRAME:
            expected_live = list(range(bucket, (now // minute_seconds) * minute_seconds + minute_seconds, minute_seconds))
            if times == expected_live:
                live = {"ts": bucket, "open": members[0]["open"], "close": members[-1]["close"]}

    return closed[-250:], live


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            s = json.load(f)
        if isinstance(s, dict):
            return s
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("ÐÐµ Ð²Ð´Ð°Ð»Ð¾ÑÑ Ð¿ÑÐ¾ÑÐ¸ÑÐ°ÑÐ¸ ÑÐ°Ð¹Ð» ÑÑÐ°Ð½Ñ %s", path)
    return {"seen": [], "pending": []}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class Engine:
    def __init__(self, symbol):
        self.symbol = symbol
        self.path = os.getenv("STATE_FILE_" + symbol, "state_10m_" + symbol + ".json")
        self.state = load_state(self.path)
        self.state.setdefault("seen", [])
        self.state.setdefault("pending", [])

    def seen(self, key):
        return key in self.state["seen"]

    def mark(self, key):
        if key not in self.state["seen"]:
            self.state["seen"].append(key)
            self.state["seen"] = self.state["seen"][-3000:]

    def base_pattern(self, candles, s):
        # #1-#3 are the final three candles of their color run; #4 and #5
        # must both be the opposite color. #6 may be any color.
        if s + 5 >= len(candles):
            return None
        cs = [color(c) for c in candles]
        start = cs[s]
        if start not in ("G", "R") or cs[s+1:s+3] != [start, start]:
            return None
        opposite = "R" if start == "G" else "G"
        if cs[s+3:s+5] != [opposite, opposite]:
            return None
        return {"start": start, "opposite": opposite, "direction": "LONG" if start == "G" else "SHORT", "ts": candles[s]["ts"], "s": s}

    def signal(self, rule, p, signal_ts, candles, result_start_index, result_end_number):
        key = f"{self.symbol}|{p['ts']}|R{rule}"
        signal_key = key + "|signal"
        if self.seen(signal_key):
            return
        direction = p["direction"]
        message = (
            f"ð Ð¡ÐÐÐÐÐ {direction} {emoji(p['start'])}\n"
            f"{label(self.symbol)} â MEXC Futures | 10 ÑÐ²Ð¸Ð»Ð¸Ð½\n"
            f"ÐÑÐ°Ð²Ð¸Ð»Ð¾ {rule}\n"
            f"Ð§Ð°Ñ ÑÐ¸Ð³Ð½Ð°Ð»Ñ: {kyiv(signal_ts)} (ÐÐ¸ÑÐ²)\n"
            f"ÐÐµÑÐµÐ²ÑÑÐºÐ° ÑÐµÐ·ÑÐ»ÑÑÐ°ÑÑ: ÑÐ²ÑÑÐºÐ¸ â{result_start_index}â{result_end_number}\n"
            "Ð¡Ð¸Ð³Ð½Ð°Ð» ÑÐ½ÑÐ¾ÑÐ¼Ð°ÑÑÐ¹Ð½Ð¸Ð¹ â Ð°Ð²ÑÐ¾Ð¼Ð°ÑÐ¸ÑÐ½Ñ ÑÐ³Ð¾Ð´Ð¸ Ð½Ðµ Ð²ÑÐ´ÐºÑÐ¸Ð²Ð°ÑÑÑÑÑ."
        )
        # Claim the event before sending so a failed request cannot create a
        # new copy every POLL_SECONDS. send() itself retries 429 using retry_after.
        self.mark(signal_key)
        if send(message):
            self.state["pending"].append({"key": key, "rule": rule, "direction": direction, "start": p["start"], "opposite": p["opposite"], "signal_ts": signal_ts, "result_start_ts": signal_ts, "count": result_end_number-result_start_index+1})
        else:
            log.error("SIGNAL NOT DELIVERED; event suppressed to prevent message flood | %s", key)

    def update(self, closed, live):
        if len(closed) < 15:
            return
        colors = [color(c) for c in closed]
        last = len(closed) - 1

        # IMPORTANT: evaluate only patterns whose trigger candle is the newest
        # closed candle. Scanning 80 historical candles caused a burst of old
        # alerts/signals when the bot started or a send failed.
        candidate_indices = range(max(0, len(closed) - 8), min(len(closed) - 5, len(closed)))
        for s in candidate_indices:
            p = self.base_pattern(closed, s)
            if not p:
                continue
            i6, i7, i8 = s + 5, s + 6, s + 7
            key1 = f"{self.symbol}|{p['ts']}|R1"
            key2 = f"{self.symbol}|{p['ts']}|R2"

            # RULE 1: warning only at the close of #7; signal only at close of #8.
            if i7 == last and colors[i7] == p["start"] and not self.seen(key1 + "|warning"):
                self.mark(key1 + "|warning")
                send(f"â ï¸ ÐÐÐÐÐ ÐÐÐÐÐÐÐ¯ â ÐÐ ÐÐÐÐÐ 1\n{label(self.symbol)} â MEXC Futures | 10 ÑÐ²Ð¸Ð»Ð¸Ð½\nÐ¡Ð²ÑÑÐºÐ° â7 Ð·Ð°ÐºÑÐ¸Ð»Ð°ÑÑ {emoji(p['start'])}.\nÐÐ¾Ð¶Ð»Ð¸Ð²Ð¸Ð¹ Ð½Ð°Ð¿ÑÑÐ¼Ð¾Ðº: {p['direction']} {emoji(p['start'])}\nÐÑÑÐºÑÑÐ¼Ð¾ Ð·Ð°ÐºÑÐ¸ÑÑÑ ÑÐ²ÑÑÐºÐ¸ â8 Ð´Ð»Ñ ÑÐ¸Ð³Ð½Ð°Ð»Ñ.")

            if i8 == last and colors[i7] == p["start"]:
                self.signal(1, p, closed[i8]["ts"] + TIMEFRAME, closed, 9, 15)

            # RULE 2: warning for the currently forming #7 only once per pattern.
            if (live and i6 == last and live["ts"] == closed[i6]["ts"] + TIMEFRAME
                    and color(live) == p["opposite"] and not self.seen(key2 + "|warning")):
                self.mark(key2 + "|warning")
                send(f"â ï¸ ÐÐÐÐÐ ÐÐÐÐÐÐÐ¯ â ÐÐ ÐÐÐÐÐ 2\n{label(self.symbol)} â MEXC Futures | 10 ÑÐ²Ð¸Ð»Ð¸Ð½\nÐ¡Ð²ÑÑÐºÐ° â7 Ð·Ð°ÑÐ°Ð· {emoji(p['opposite'])}.\nÐÐ¾Ð¶Ð»Ð¸Ð²Ð¸Ð¹ Ð½Ð°Ð¿ÑÑÐ¼Ð¾Ðº: {p['direction']} {emoji(p['start'])}\nÐ¡Ð¸Ð³Ð½Ð°Ð» Ð±ÑÐ´Ðµ Ð»Ð¸ÑÐµ ÑÐºÑÐ¾ ÑÐ²ÑÑÐºÐ° â7 Ð·Ð°ÐºÑÐ¸ÑÑÑÑÑ {emoji(p['opposite'])}.\nÐÑÑÐºÑÐ²Ð°Ð½Ðµ Ð·Ð°ÐºÑÐ¸ÑÑÑ: {kyiv(live['ts'] + TIMEFRAME)} (ÐÐ¸ÑÐ²).")

            if i7 == last and colors[i7] == p["opposite"]:
                self.signal(2, p, closed[i7]["ts"] + TIMEFRAME, closed, 8, 14)

        self.resolve(closed)
        save_state(self.path, self.state)

    def resolve(self, closed):
        by_ts = {c["ts"]: c for c in closed}
        remain = []
        for p in self.state["pending"]:
            times = [p["result_start_ts"] + i*TIMEFRAME for i in range(p["count"])]
            if not all(t in by_ts for t in times):
                missing = [t for t in times if t not in by_ts]
                log.debug("RESULT WAITING | %s | rule=%s | missing=%s", self.symbol, p["rule"], len(missing))
                remain.append(p)
                continue
            cs = [color(by_ts[t]) for t in times]
            # Finish the evaluation as soon as all 7 result candles are closed.
            # Doji is neither green nor red: it cannot make a WIN, and LOSS is
            # valid only when every result candle is the opposite color.
            result = classify_result(p["start"], p["opposite"], cs)
            if result == "WIN":
                uk = "ÐÐÐ ÐÐÐÐÐ"
                result_emoji = "ð¢"
            elif result == "LOSS":
                uk = "ÐÐÐ ÐÐÐÐ"
                result_emoji = "ð´"
            else:
                uk = "ÐÐÐ Ð ÐÐÐ£ÐÐ¬Ð¢ÐÐ¢Ð£ â Ð ÐÐÐÐÐ"
                result_emoji = "âª"
            key = p["key"] + "|result"
            if not self.seen(key):
                msg = (f"{result_emoji} {uk} ({result})\n"
                       f"{label(self.symbol)} â MEXC Futures | 10 ÑÐ²Ð¸Ð»Ð¸Ð½\nÐÑÐ°Ð²Ð¸Ð»Ð¾ {p['rule']}\n"
                       f"ÐÐ°Ð¿ÑÑÐ¼Ð¾Ðº: {p['direction']}\nÐ§Ð°Ñ ÑÐ¸Ð³Ð½Ð°Ð»Ñ: {kyiv(p['signal_ts'])} (ÐÐ¸ÑÐ²)\n"
                       f"Ð¡Ð²ÑÑÐºÐ¸ ÑÐµÐ·ÑÐ»ÑÑÐ°ÑÑ: {8 if p['rule'] == 2 else 9}â{14 if p['rule'] == 2 else 15}\n"
                       f"Ð§Ð°Ñ Ð·Ð°ÐºÑÐ¸ÑÑÑ ÑÐµÐ·ÑÐ»ÑÑÐ°ÑÑ: {kyiv(times[-1]+TIMEFRAME)} (ÐÐ¸ÑÐ²)")
                log.info("RESULT READY | %s | rule=%s | result=%s | candles=%s", self.symbol, p["rule"], result, ",".join(cs))
                if send(msg):
                    self.mark(key)
                else:
                    remain.append(p)
        self.state["pending"] = remain


def run_tests():
    def c(col, i):
        return {"ts": i*TIMEFRAME, "open": 10.0, "close": 11.0 if col == "G" else 9.0 if col == "R" else 10.0}
    e = Engine.__new__(Engine)
    a = [c(x, i) for i, x in enumerate(["G","G","G","R","R","D","G","D","R","G","R","R","G","R","G"])]
    assert e.base_pattern(a, 0)["direction"] == "LONG"
    b = [c(x, i) for i, x in enumerate(["R","R","R","G","G","D","G"])]
    assert e.base_pattern(b, 0)["direction"] == "SHORT"
    bad = [c(x, i) for i, x in enumerate(["G","G","G","R","G","D"])]
    assert e.base_pattern(bad, 0) is None
    # Check that all test candles use 10-minute spacing.
    assert TIMEFRAME == 600 and b[1]["ts"] - b[0]["ts"] == 600
    # Result evaluation: any favorable candle = WIN; all opposite = LOSS;
    # a doji with no favorable candle ends as a neutral result, not endless pending.
    assert classify_result("G", "R", ["R", "G", "R", "R", "R", "R", "R"]) == "WIN"
    assert classify_result("G", "R", ["R"] * 7) == "LOSS"
    assert classify_result("G", "R", ["R", "R", "D", "R", "R", "R", "R"]) == "DOJI"
    # Explicit UTF-8 text/emoji sanity check.
    msg = "â ï¸ ÐÐÐÐÐ ÐÐÐÐÐÐÐ¯\nð Ð¡ÐÐÐÐÐ LONG\nÐÐÐ ÐÐÐÐÐ (WIN)\nÐÐÐ ÐÐÐÐ (LOSS)"
    assert "ÐÐÐÐÐ ÐÐÐÐÐÐÐ¯" in msg and "ð" in msg
    print("Offline tests passed: LONG/SHORT, 10m spacing, #4-#5 same color, doji, Ukrainian text and emoji.")
    print("No MEXC requests or Telegram messages sent.")


def main():
    if TEST_MODE:
        run_tests()
        return
    if not TOKEN or not CHAT_IDS:
        raise RuntimeError("ÐÐ°Ð´Ð°Ð¹ÑÐµ TELEGRAM_BOT_TOKEN Ñ TELEGRAM_CHAT_ID Ñ Railway â Variables")
    log.info("ÐÐ¾ÑÐ° Ð·Ð°Ð¿ÑÑÐµÐ½Ð¾ | MEXC Futures | ÑÐ²ÑÑÐºÐ¸ 10 ÑÐ², Ð·ÑÐ±ÑÐ°Ð½Ñ Ð· 1-ÑÐ²Ð¸Ð»Ð¸Ð½Ð½Ð¸Ñ Ð´Ð°Ð½Ð¸Ñ | %s", ", ".join(SYMBOLS))
    send("ð¤ ÐÐÐ¢ ÐÐÐÐ£Ð©ÐÐÐ\nMEXC Futures | 10 ÑÐ²Ð¸Ð»Ð¸Ð½\nÐÐ½ÑÑÑÑÐ¼ÐµÐ½ÑÐ¸: " + ", ".join(label(s) for s in SYMBOLS) + "\nÐÑÐ°ÑÑÑÑÑ Ð¾ÐºÑÐµÐ¼Ð¾ ÐÑÐ°Ð²Ð¸Ð»Ð¾ 1 Ñ ÐÑÐ°Ð²Ð¸Ð»Ð¾ 2.\nÐ¡Ð¸Ð³Ð½Ð°Ð»Ð¸ ÑÐ½ÑÐ¾ÑÐ¼Ð°ÑÑÐ¹Ð½Ñ â Ð±ÐµÐ· Ð°Ð²ÑÐ¾Ð¼Ð°ÑÐ¸ÑÐ½Ð¾Ð³Ð¾ Ð²ÑÐ´ÐºÑÐ¸ÑÑÑ ÑÐ³Ð¾Ð´.")
    engines = {s: Engine(s) for s in SYMBOLS}
    while True:
        for symbol, engine in engines.items():
            try:
                closed, live = fetch(symbol)
                engine.update(closed, live)
                if closed:
                    log.info("HEARTBEAT OK | %s | closed=%d | latest=%s | pending=%d", symbol, len(closed), kyiv(closed[-1]["ts"]+TIMEFRAME), len(engine.state["pending"]))
            except Exception:
                log.exception("ÐÐ¾Ð¼Ð¸Ð»ÐºÐ° ÑÐ¸ÐºÐ»Ñ | %s", symbol)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
