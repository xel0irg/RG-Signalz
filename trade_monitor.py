#!/usr/bin/env python3
"""
PRISMA GROUP Trade Monitor -> Telegram Notifier
Optimized for GitHub Actions: runs for ~9 minutes, checks every 12s.
State is passed via environment variable between runs using GitHub Actions cache.
"""

import os, sys, json, time, html, signal
from pathlib import Path
import requests

# ── CONFIG ──────────────────────────────────────────────────────────────────
API_URL            = "https://api.prismagroup.online/api/trades/recent?limit=500&period=current_week"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
POLL_SECONDS       = 12
REQUEST_TIMEOUT    = 15
RUN_DURATION       = 30 * 60   # run for 30 minutes, then exit cleanly
STATE_FILE         = Path(os.environ.get("STATE_FILE", "trade_state.json"))
WATCH_FIELDS       = ["status", "profit_dollars", "profit_percentage",
                      "highest_price", "exit_price", "profit_per_contract"]
# ────────────────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            log(f"Telegram error {r.status_code}: {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        log(f"Telegram request failed: {e}")
        return False

def fetch_trades():
    try:
        r = requests.get(API_URL, timeout=REQUEST_TIMEOUT,
                         headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            log(f"Unexpected response shape: {type(data)}")
            return None
        return data
    except requests.RequestException as e:
        log(f"Fetch failed: {e}")
        return None
    except json.JSONDecodeError as e:
        log(f"Bad JSON: {e}")
        return None

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as e:
            log(f"Could not read state file, starting fresh: {e}")
    return {}

def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state))
    except OSError as e:
        log(f"Could not write state file: {e}")

def fmt_money(v):
    try: return f"${float(v):,.2f}"
    except: return str(v)

def fmt_pct(v):
    try: return f"{float(v):.2f}%"
    except: return str(v)

def trade_header(t):
    sym    = t.get("symbol", "?")
    strike = t.get("strike", "?")
    otype  = t.get("option_type", "")
    exp    = t.get("expiration", "")
    return f"{html.escape(str(sym))} ${strike} {html.escape(str(otype))} (exp {html.escape(str(exp))})"

def msg_new_trade(t):
    lines = [
        "🟢 <b>NEW TRADE OPENED</b>",
        trade_header(t),
        f"Entry: {fmt_money(t.get('entry_price'))}",
        f"Status: {html.escape(str(t.get('status', '—')))}",
    ]
    if t.get("signal_source"):
        lines.append(f"Source: {html.escape(str(t['signal_source']))}")
    return "\n".join(lines)

def msg_updated_trade(t, changes):
    status = str(t.get("status", "")).upper()
    if status == "WIN":                              icon = "✅"
    elif status in ("LOSS","LOSE","STOP","STOPPED"): icon = "🔴"
    else:                                            icon = "🔄"
    lines = [f"{icon} <b>TRADE UPDATED</b>", trade_header(t)]
    for field, (old, new) in changes.items():
        if field == "profit_dollars":      old_s, new_s = fmt_money(old), fmt_money(new)
        elif field == "profit_percentage": old_s, new_s = fmt_pct(old), fmt_pct(new)
        else:                              old_s, new_s = html.escape(str(old)), html.escape(str(new))
        lines.append(f"{field.replace('_',' ').title()}: {old_s} → {new_s}")
    return "\n".join(lines)

def diff_changes(old_trade, new_trade):
    changes = {}
    for f in WATCH_FIELDS:
        if f in new_trade and old_trade.get(f) != new_trade.get(f):
            changes[f] = (old_trade.get(f), new_trade.get(f))
    return changes

def run():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
        sys.exit(1)

    state     = load_state()
    first_run = len(state) == 0

    stop = {"flag": False}
    def handle_sig(signum, frame):
        stop["flag"] = True
        log("Shutdown signal received, exiting after current cycle...")
    signal.signal(signal.SIGINT,  handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    start_time = time.time()
    log(f"Starting. Polling every {POLL_SECONDS}s for {RUN_DURATION//60} minutes.")
    if first_run:
        log("First run: recording current trades silently (no backlog spam).")

    consecutive_failures = 0

    while not stop["flag"]:
        # Exit cleanly before GitHub Actions kills us
        if time.time() - start_time >= RUN_DURATION:
            log("Run duration reached, exiting cleanly.")
            break

        trades = fetch_trades()

        if trades is None:
            consecutive_failures += 1
            backoff = min(POLL_SECONDS * consecutive_failures, 120)
            if consecutive_failures == 3:
                send_telegram("⚠️ Trade monitor: the API has been unreachable "
                              "for a few cycles. Still retrying.")
            time.sleep(backoff)
            continue

        if consecutive_failures >= 3:
            send_telegram("✅ Trade monitor: API reachable again.")
        consecutive_failures = 0

        new_state     = {}
        notifications = []

        for t in trades:
            tid = str(t.get("id"))
            if tid == "None":
                continue
            keep = {k: t.get(k) for k in
                    ["id","trade_id","symbol","strike","option_type",
                     "expiration","entry_price","signal_source"] + WATCH_FIELDS}
            new_state[tid] = keep

            if first_run:
                continue

            if tid not in state:
                notifications.append((t.get("created_at",""), msg_new_trade(t)))
            else:
                changes = diff_changes(state[tid], t)
                if changes:
                    notifications.append((t.get("updated_at",""), msg_updated_trade(t, changes)))

        notifications.sort(key=lambda x: x[0])
        for _, text in notifications:
            send_telegram(text)

        if notifications:
            log(f"Sent {len(notifications)} notification(s).")

        state = new_state
        save_state(state)
        first_run = False

        slept = 0
        while slept < POLL_SECONDS and not stop["flag"]:
            time.sleep(1)
            slept += 1

    log("Stopped.")

if __name__ == "__main__":
    run()
