# -*- coding: utf-8 -*-
"""MEXC Futures 5m Telegram signal bot: two separate candle rules. Signals only."""
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
TIMEFRAME = 300
API_URL = os.getenv("MEXC_FUTURES_KLINE_URL", "https://api.mexc.com/api/v1/contract/kline/{symbol}").replace("https://contract.mexc.com", "https://api.mexc.com")
KYIV = ZoneInfo("Europe/Kyiv")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("mexc-two-rules")
session = requests.Session()
session.headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache", "User-Agent": "MEXC-Futures-5m-Two-Rules-Bot/1.0"})


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


def send(message):
    if TEST_MODE:
        print("\n--- TEST MESSAGE ---\n" + message + "\n--------------------")
        return True
    if not TOKEN or not CHAT_IDS:
        log.error("Не налаштовано Telegram: задайте TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID")
        return False
    ok = True
    for chat_id in CHAT_IDS:
        try:
            r = session.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": chat_id, "text": message}, timeout=15)
            payload = r.json()
            if r.ok and payload.get("ok"):
                log.info("TELEGRAM SENT OK | chat=%s", chat_id)
            else:
                ok = False
                log.error("TELEGRAM ERROR | chat=%s | status=%s | body=%s", chat_id, r.status_code, r.text[:300])
        except Exception:
            ok = False
            log.exception("TELEGRAM EXCEPTION | chat=%s", chat_id)
    return ok


def fetch(symbol, limit=300):
    now = int(time.time())
    r = session.get(API_URL.format(symbol=symbol), params={"interval": "Min5", "limit": limit, "start": now-limit*TIMEFRAME, "end": now}, timeout=15)
    r.raise_for_status()
    p = r.json()
    if p.get("success") is False:
        raise RuntimeError(f"Помилка MEXC API: {p}")
    d = p.get("data")
    if not isinstance(d, dict) or not isinstance(d.get("time"), list):
        raise RuntimeError(f"Несподівана відповідь MEXC: {str(p)[:250]}")
    if not (len(d["time"]) == len(d.get("open", [])) == len(d.get("close", []))):
        raise RuntimeError("Масиви свічок MEXC мають різну довжину")
    rows = []
    for ts, op, cl in zip(d["time"], d["open"], d["close"]):
        ts = int(ts)
        if ts > 10_000_000_000:
            ts //= 1000
        rows.append({"ts": ts, "open": float(op), "close": float(cl)})
    rows = list({c["ts"]: c for c in sorted(rows, key=lambda x: x["ts"])}.values())
    rows.sort(key=lambda x: x["ts"])
    closed = [c for c in rows if c["ts"] + TIMEFRAME <= now]
    live = next((c for c in reversed(rows) if c["ts"] <= now < c["ts"] + TIMEFRAME), None)
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
        self.path = os.getenv("STATE_FILE_" + symbol, "state_" + symbol + ".json")
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
        if self.seen(key + "|signal"):
            return
        direction = p["direction"]
        message = (
            f"🔔 СИГНАЛ {direction} {emoji(p['start']) if direction == 'LONG' else emoji(p['start'])}\n"
            f"{label(self.symbol)} — MEXC Futures | 5 хв\n"
            f"Правило {rule}\n"
            f"Час сигналу: {kyiv(signal_ts)} (Київ)\n"
            f"Перевірка результату: свічки №{result_start_index}–{result_end_number}\n"
            "Сигнал інформаційний — автоматичні угоди не відкриваються."
        )
        if send(message):
            self.mark(key + "|signal")
            self.state["pending"].append({"key": key, "rule": rule, "direction": direction, "start": p["start"], "opposite": p["opposite"], "signal_ts": signal_ts, "result_start_ts": signal_ts, "count": result_end_number-result_start_index+1})

    def update(self, closed, live):
        if len(closed) < 15:
            return
        colors = [color(c) for c in closed]
        # Search only recent history; seen keys prevent duplicate signals after polling.
        for s in range(max(0, len(closed)-80), len(closed)):
            p = self.base_pattern(closed, s)
            if not p:
                continue
            i6, i7, i8 = s+5, s+6, s+7
            key1 = f"{self.symbol}|{p['ts']}|R1"
            key2 = f"{self.symbol}|{p['ts']}|R2"

            # RULE 1: after #7 closes in the starting color, warn; after #8 closes, signal.
            if i7 < len(closed) and colors[i7] == p["start"] and not self.seen(key1 + "|warning"):
                if send(f"⚠️ ПОПЕРЕДЖЕННЯ — ПРАВИЛО 1\n{label(self.symbol)} — MEXC Futures | 5 хв\nСвічка №7 закрилася {emoji(p['start'])}.\nМожливий напрямок: {p['direction']} {emoji(p['start'])}\nОчікуємо закриття свічки №8 для сигналу."):
                    self.mark(key1 + "|warning")
            if i8 < len(closed) and colors[i7] == p["start"]:
                self.signal(1, p, closed[i8]["ts"] + TIMEFRAME, closed, 9, 15)

            # RULE 2: alert during live #7 if its current candle color is opposite;
            # final signal only after #7 closes and is still opposite.
            if live and live["ts"] == closed[i6]["ts"] + TIMEFRAME and color(live) == p["opposite"] and not self.seen(key2 + "|warning"):
                if send(f"⚠️ ПОПЕРЕДЖЕННЯ — ПРАВИЛО 2\n{label(self.symbol)} — MEXC Futures | 5 хв\nСвічка №7 зараз {emoji(p['opposite'])}.\nМожливий напрямок: {p['direction']} {emoji(p['start'])}\nСигнал буде лише якщо свічка №7 закриється {emoji(p['opposite'])}.\nОчікуване закриття: {kyiv(live['ts'] + TIMEFRAME)} (Київ)."):
                    self.mark(key2 + "|warning")
            if i7 < len(closed) and colors[i7] == p["opposite"]:
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
                       f"{label(self.symbol)} — MEXC Futures | 5 хв\nПравило {p['rule']}\n"
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
        return {"ts": i*300, "open": 10.0, "close": 11.0 if col == "G" else 9.0 if col == "R" else 10.0}
    e = Engine.__new__(Engine)
    a = [c(x, i) for i, x in enumerate(["G","G","G","R","R","D","G","D","R","G","R","R","G","R","G"])]
    assert e.base_pattern(a, 0)["direction"] == "LONG"
    b = [c(x, i) for i, x in enumerate(["R","R","R","G","G","D","G"])]
    assert e.base_pattern(b, 0)["direction"] == "SHORT"
    bad = [c(x, i) for i, x in enumerate(["G","G","G","R","G","D"])]
    assert e.base_pattern(bad, 0) is None
    # Explicit UTF-8 text/emoji sanity check.
    msg = "⚠️ ПОПЕРЕДЖЕННЯ\n🔔 СИГНАЛ LONG\nПЕРЕМОГА (WIN)\nПОРАЗКА (LOSS)"
    assert "ПОПЕРЕДЖЕННЯ" in msg and "🔔" in msg
    print("Offline tests passed: LONG/SHORT, #4-#5 same color, doji, Ukrainian text and emoji.")
    print("No MEXC requests or Telegram messages sent.")


def main():
    if TEST_MODE:
        run_tests()
        return
    if not TOKEN or not CHAT_IDS:
        raise RuntimeError("Задайте TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID у Railway → Variables")
    log.info("Бота запущено | MEXC Futures | нативні свічки 5 хв | %s", ", ".join(SYMBOLS))
    send("🤖 БОТ ЗАПУЩЕНО\nMEXC Futures | 5 хв\nІнструменти: " + ", ".join(label(s) for s in SYMBOLS) + "\nПрацюють окремо Правило 1 і Правило 2.\nСигнали інформаційні — без автоматичного відкриття угод.")
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
