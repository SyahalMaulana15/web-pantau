#!/usr/bin/env python3
"""
JKT48 Ticket Monitor — Flask Web Dashboard
"""

import requests
import os
import time
import threading
from datetime import datetime, timezone, timedelta
from collections import deque
from flask import Flask, jsonify, render_template

# ─────────────────────────────────────────────
#  KONFIGURASI
# ─────────────────────────────────────────────

API_URL        = "https://jkt48.com/api/v1/exclusives/EXE588/bonus?lang=id"
EXCLUSIVE_CODE = "EXE588"
BOT_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID        = os.environ.get("TELEGRAM_CHAT_ID", "")
INTERVAL       = 30
HEARTBEAT_H    = 6
MAX_FAIL       = 5
WATCH_MEMBERS  = []
MAX_LOG        = 100  # Maks entri log history

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "id-ID,id;q=0.9",
    "Referer": "https://jkt48.com/",
    "Origin": "https://jkt48.com",
}

app = Flask(__name__)

# ─────────────────────────────────────────────
#  STATE GLOBAL
# ─────────────────────────────────────────────

state = {
    "running": False,
    "run_count": 0,
    "fail_count": 0,
    "fail_total": 0,
    "start_time": None,
    "last_check": None,
    "last_heartbeat": None,
    "prev_quota": {},
    "sessions": [],
    "change_log": deque(maxlen=MAX_LOG),  # log perubahan quota
    "notif_total": 0,
}

# ─────────────────────────────────────────────
#  HELPER
# ─────────────────────────────────────────────

def wib():
    return datetime.now(timezone(timedelta(hours=7)))

def wib_str():
    return wib().strftime("%Y-%m-%d %H:%M:%S WIB")

def telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10
        ).raise_for_status()
    except Exception as e:
        print(f"  ❌ Gagal kirim Telegram: {e}")

def fetch(retries=3):
    for i in range(1, retries + 1):
        try:
            r = requests.get(API_URL, headers=HEADERS, timeout=20)
            r.raise_for_status()
            text = r.content.decode("utf-8", errors="replace").strip()
            if not text or not (text.startswith("{") or text.startswith("[")):
                raise ValueError(f"Respons tidak valid: {text[:80]!r}")
            data = r.json()
            if data.get("status") and "data" in data:
                return data["data"]
        except Exception as e:
            if i < retries:
                time.sleep(i * 15)
            else:
                print(f"  ❌ Fetch gagal {retries}x: {e}")
    return None

def extract_quota(sessions):
    return {
        str(m.get("session_detail_id", "")): m.get("quota", 0)
        for s in sessions for m in s.get("session_members", [])
    }

# ─────────────────────────────────────────────
#  MONITOR THREAD
# ─────────────────────────────────────────────

def monitor_loop():
    purchase_url = f"https://jkt48.com/purchase/exclusive?code={EXCLUSIVE_CODE}"

    # Init data awal
    while True:
        sessions = fetch()
        if sessions:
            break
        time.sleep(15)

    state["sessions"]       = sessions
    state["prev_quota"]     = extract_quota(sessions)
    state["start_time"]     = wib()
    state["last_heartbeat"] = wib()
    state["last_check"]     = wib()
    state["running"]        = True

    telegram(
        f"✅ <b>JKT48 Monitor aktif!</b>\n"
        f"🔔 Notif saat tiket <b>berkurang</b>\n"
        f"⚡ Cek setiap <b>{INTERVAL} detik</b> | 🕐 {wib_str()}"
    )

    while state["running"]:
        time.sleep(INTERVAL)
        state["run_count"] += 1
        state["last_check"] = wib()

        sessions = fetch()
        if not sessions:
            state["fail_count"] += 1
            state["fail_total"] += 1
            if state["fail_count"] == MAX_FAIL:
                telegram(f"⚠️ <b>API Bermasalah</b> — Gagal {MAX_FAIL}x berturut-turut\n🕐 {wib_str()}")
            continue

        state["fail_count"] = 0
        state["sessions"]   = sessions
        new_quota           = extract_quota(sessions)

        for s in sessions:
            label, stime = s.get("label", "?"), s.get("start_time", "")[:5]
            for m in s.get("session_members", []):
                name      = m.get("member_name", "")
                jalur     = m.get("label", "")
                quota     = m.get("quota", 0)
                price     = m.get("price", 0)
                did       = str(m.get("session_detail_id", ""))

                if WATCH_MEMBERS and name not in WATCH_MEMBERS:
                    continue

                prev    = state["prev_quota"].get(did, 0)
                selisih = prev - quota

                if selisih > 0:
                    # Catat ke log
                    state["change_log"].appendleft({
                        "time":    wib().strftime("%H:%M:%S"),
                        "date":    wib().strftime("%Y-%m-%d"),
                        "type":    "berkurang",
                        "name":    name,
                        "sesi":    f"{label} ({stime} WIB)",
                        "jalur":   jalur,
                        "before":  prev,
                        "after":   quota,
                        "selisih": selisih,
                        "price":   price,
                    })
                    state["notif_total"] += 1

                    icon = "🔴" if quota == 0 else ("🟡" if (quota / (quota + selisih)) < 0.3 else "🟢")
                    telegram(
                        f"🛒 <b>TIKET TERBELI!</b>\n\n"
                        f"👤 <b>{name}</b> | 📋 {label} ({stime} WIB)\n"
                        f"🚪 {jalur} | 💰 Rp{price:,}\n"
                        f"📉 {prev} → {quota} <i>(-{selisih})</i> | {icon} Sisa: {quota}"
                        + (" <i>(SOLD OUT!)</i>" if quota == 0 else "") +
                        f"\n🕐 {wib_str()}\n🔗 <a href='{purchase_url}'>Lihat tiket →</a>"
                    )

                elif quota > prev:
                    state["change_log"].appendleft({
                        "time":    wib().strftime("%H:%M:%S"),
                        "date":    wib().strftime("%Y-%m-%d"),
                        "type":    "restock",
                        "name":    name,
                        "sesi":    f"{label} ({stime} WIB)",
                        "jalur":   jalur,
                        "before":  prev,
                        "after":   quota,
                        "selisih": quota - prev,
                        "price":   price,
                    })

        state["prev_quota"] = new_quota

        # Heartbeat
        if (wib() - state["last_heartbeat"]).total_seconds() >= HEARTBEAT_H * 3600:
            total = sum(len(s.get("session_members", [])) for s in sessions)
            avail = sum(1 for s in sessions for m in s.get("session_members", []) if m.get("quota", 0) > 0)
            now   = wib()
            telegram(
                f"💓 <b>Laporan Berkala</b>\n\n"
                f"🕐 {now.strftime('%Y-%m-%d %H:%M WIB')} | ⚡ Interval: {INTERVAL}s\n"
                f"📊 Total: {total} | Tersedia: {avail} | Sold out: {total - avail}\n"
                f"🔁 Berikutnya: {(now + timedelta(hours=HEARTBEAT_H)).strftime('%H:%M WIB')} | 📈 Cek: {state['run_count']:,}x"
            )
            state["last_heartbeat"] = now

# ─────────────────────────────────────────────
#  FLASK ROUTES
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    now     = wib()
    uptime  = str(now - state["start_time"]).split(".")[0] if state["start_time"] else "-"
    sessions = state["sessions"]
    members = []

    for s in sessions:
        label = s.get("label", "?")
        stime = s.get("start_time", "")[:5]
        for m in s.get("session_members", []):
            quota = m.get("quota", 0)
            prev  = state["prev_quota"].get(str(m.get("session_detail_id", "")), 0)
            members.append({
                "name":  m.get("member_name", ""),
                "sesi":  f"{label} ({stime} WIB)",
                "jalur": m.get("label", ""),
                "quota": quota,
                "price": m.get("price", 0),
                "status": "sold_out" if quota == 0 else ("low" if quota <= 3 else "available"),
            })

    total = len(members)
    avail = sum(1 for m in members if m["quota"] > 0)

    return jsonify({
        "running":      state["running"],
        "run_count":    state["run_count"],
        "fail_total":   state["fail_total"],
        "notif_total":  state["notif_total"],
        "uptime":       uptime,
        "last_check":   state["last_check"].strftime("%H:%M:%S WIB") if state["last_check"] else "-",
        "last_hb":      state["last_heartbeat"].strftime("%H:%M:%S WIB") if state["last_heartbeat"] else "-",
        "total_slots":  total,
        "avail_slots":  avail,
        "members":      members,
        "interval":     INTERVAL,
    })

@app.route("/api/log")
def api_log():
    return jsonify(list(state["change_log"]))

# ─────────────────────────────────────────────
#  ENTRYPOINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
