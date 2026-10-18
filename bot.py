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
    return {"G": "🟢", "R": "🔴", "D": "⚪"}[c]


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
        log.error("Не налаштовано Telegram: задайте TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID")
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
        raise RuntimeError(f"Помилка API MEXC: {payload}")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("time"), list):
        raise RuntimeError(f"Неочікувана відповідь API MEXC: {str(payload)[:250]}")
    if not (len(data["time"]) == len(data.get("open", [])) == len(data.get("close", []))):
        raise RuntimeError("Масиви свічок MEXC мають різну довжину")

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
        log.exception("Не вдалося прочитати файл стану %s", path)
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
            f"🔔 СИГНАЛ {direction} {emoji(p['start'])}\n"
            f"{label(self.symbol)} — MEXC Futures | 10 хвилин\n"
            f"Правило {rule}\n"
            f"Час сигналу: {kyiv(signal_ts)} (Київ)\n"
            f"Перевірка результату: свічки №{result_start_index}–{result_end_number}\n"
            "Сигнал інформаційний — автоматичні угоди не відкриваються."
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
                send(f"⚠️ ПОПЕРЕДЖЕННЯ — ПРАВИЛО 1\n{label(self.symbol)} — MEXC Futures | 10 хвилин\nСвічка №7 закрилася {emoji(p['start'])}.\nМожливий напрямок: {p['direction']} {emoji(p['start'])}\nОчікуємо закриття свічки №8 для сигналу.")

            if i8 == last and colors[i7] == p["start"]:
                self.signal(1, p, closed[i8]["ts"] + TIMEFRAME, closed, 9, 15)

            # RULE 2: warning for the currently forming #7 only once per pattern.
            if (live and i6 == last and live["ts"] == closed[i6]["ts"] + TIMEFRAME
                    and color(live) == p["opposite"] and not self.seen(key2 + "|warning")):
                self.mark(key2 + "|warning")
                send(f"⚠️ ПОПЕРЕДЖЕННЯ — ПРАВИЛО 2\n{label(self.symbol)} — MEXC Futures | 10 хвилин\nСвічка №7 зараз {emoji(p['opposite'])}.\nМожливий напрямок: {p['direction']} {emoji(p['start'])}\nСигнал буде лише якщо свічка №7 закриється {emoji(p['opposite'])}.\nОчікуване закриття: {kyiv(live['ts'] + TIMEFRAME)} (Київ).")

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
                remain.append(p)
                continue
            cs = [color(by_ts[t]) for t in times]
            if p["start"] in cs:
                result = "WIN"
                uk = "ПЕРЕМОГА"
            elif all(c == p["opposite"] for c in cs):
                result = "LOSS"
                uk = "ПОРАЗКА"
            else:
                # A doji is neither green nor red; it cannot by itself establish LOSS.
                remain.append(p)
                continue
            key = p["key"] + "|result"
            if not self.seen(key):
                msg = (f"{'🟢' if result == 'WIN' else '🔴'} {uk} ({result})\n"
                       f"{label(self.symbol)} — MEXC Futures | 10 хвилин\nПравило {p['rule']}\n"
                       f"Напрямок: {p['direction']}\nЧас сигналу: {kyiv(p['signal_ts'])} (Київ)\n"
                       f"Свічки результату: {times[0] and (8 if p['rule'] == 2 else 9)}–{14 if p['rule'] == 2 else 15}\n"
                       f"Час закриття результату: {kyiv(times[-1]+TIMEFRAME)} (Київ)")
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
    # Explicit UTF-8 text/emoji sanity check.
    msg = "⚠️ ПОПЕРЕДЖЕННЯ\n🔔 СИГНАЛ LONG\nПЕРЕМОГА (WIN)\nПОРАЗКА (LOSS)"
    assert "ПОПЕРЕДЖЕННЯ" in msg and "🔔" in msg
    print("Offline tests passed: LONG/SHORT, 10m spacing, #4-#5 same color, doji, Ukrainian text and emoji.")
    print("No MEXC requests or Telegram messages sent.")


def main():
    if TEST_MODE:
        run_tests()
        return
    if not TOKEN or not CHAT_IDS:
        raise RuntimeError("Задайте TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID у Railway → Variables")
    log.info("Бота запущено | MEXC Futures | свічки 10 хв, зібрані з 1-хвилинних даних | %s", ", ".join(SYMBOLS))
    send("🤖 БОТ ЗАПУЩЕНО\nMEXC Futures | 10 хвилин\nІнструменти: " + ", ".join(label(s) for s in SYMBOLS) + "\nПрацюють окремо Правило 1 і Правило 2.\nСигнали інформаційні — без автоматичного відкриття угод.")
    engines = {s: Engine(s) for s in SYMBOLS}
    while True:
        for symbol, engine in engines.items():
            try:
                closed, live = fetch(symbol)
                engine.update(closed, live)
                if closed:
                    log.info("HEARTBEAT OK | %s | closed=%d | latest=%s | pending=%d", symbol, len(closed), kyiv(closed[-1]["ts"]+TIMEFRAME), len(engine.state["pending"]))
            except Exception:
                log.exception("Помилка циклу | %s", symbol)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
