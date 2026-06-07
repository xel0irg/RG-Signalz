#!/usr/bin/env python3
"""
PRISMA GROUP Trade Monitor -> Telegram + Discord notifier.

Polls the recent-trades API on an interval, diffs against the last seen
state, and sends a Telegram AND Discord message whenever a trade is opened,
closed, or meaningfully changed.

Setup:
  pip install requests
  Set the env vars below (or edit the CONFIG block), then run:
      python trade_monitor.py
"""

import os
import sys
import json
import time
import html
import signal
import re
from pathlib import Path

import requests

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
API_URL = "https://api.prismagroup.online/api/trades/recent?limit=500&period=current_week"

TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

POLL_SECONDS    = 12
REQUEST_TIMEOUT = 15
RUN_DURATION    = 59 * 60  # 59 minutes per run
STATE_FILE      = Path(os.environ.get("STATE_FILE", "trade_state.json"))

WATCH_FIELDS = ["status", "profit_dollars", "profit_percentage",
                "highest_price", "exit_price", "profit_per_contract"]
# ----------------------------------------------------------------------


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def send_telegram(text):
    """Send a message to Telegram. Returns True on success."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            log(f"Telegram error {r.status_code}: {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        log(f"Telegram request failed: {e}")
        return False


def send_discord(text):
    """Send a message to Discord. Returns True on success."""
    if not DISCORD_WEBHOOK_URL:
        return False
    # Convert HTML formatting to Discord markdown
    clean = text
    clean = clean.replace("<b>", "**").replace("</b>", "**")
    clean = clean.replace("<i>", "*").replace("</i>", "*")
    clean = re.sub(r"<[^>]+>", "", clean)
    payload = {"content": clean}
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code not in (200, 204):
            log(f"Discord error {r.status_code}: {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        log(f"Discord request failed: {e}")
        return False


def notify(text):
    """Send to both Telegram and Discord."""
    send_telegram(text)
    send_discord(text)


def fetch_trades():
    """Fetch current trades. Returns a list, or None on failure."""
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
        except (json.JSONDecodeError, OSError) as e:
            log(f"Could not read state file, starting fresh: {e}")
    return {}


def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state))
    except OSError as e:
        log(f"Could not write state file: {e}")


def fmt_money(v):
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def fmt_pct(v):
    try:
        return f"{float(v):.2f}%"
    except (TypeError, ValueError):
        return str(v)


def trade_header(t):
    sym = t.get("symbol", "?")
    strike = t.get("strike", "?")
    otype = t.get("option_type", "")
    exp = t.get("expiration", "")
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
    if status == "WIN":
        icon = "✅"
    elif status in ("LOSS", "LOSE", "STOP", "STOPPED"):
        icon = "🔴"
    else:
        icon = "🔄"

    lines = [f"{icon} <b>TRADE UPDATED</b>", trade_header(t)]
    for field, (old, new) in changes.items():
        if field in ("profit_dollars",):
            old_s, new_s = fmt_money(old), fmt_money(new)
        elif field == "profit_percentage":
            old_s, new_s = fmt_pct(old), fmt_pct(new)
        else:
            old_s, new_s = html.escape(str(old)), html.escape(str(new))
        pretty = field.replace("_", " ").title()
        lines.append(f"{pretty}: {old_s} → {new_s}")
    return "\n".join(lines)


def diff_changes(old_trade, new_trade):
    """Return {field: (old, new)} for watched fields that changed."""
    changes = {}
    for f in WATCH_FIELDS:
        if f in new_trade and old_trade.get(f) != new_trade.get(f):
            changes[f] = (old_trade.get(f), new_trade.get(f))
    return changes


def run():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
        log("Set them as environment variables or edit the CONFIG block.")
        sys.exit(1)

    state = load_state()
    first_run = len(state) == 0

    stop = {"flag": False}
    def handle_sig(signum, frame):
        stop["flag"] = True
        log("Shutdown signal received, exiting after current cycle...")
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    start_time = time.time()
    log(f"Starting. Polling every {POLL_SECONDS}s. State file: {STATE_FILE.resolve()}")
    if first_run:
        log("First run: recording current trades silently (no backlog spam).")

    consecutive_failures = 0

    while not stop["flag"]:
        if time.time() - start_time >= RUN_DURATION:
            log("Run duration reached, exiting cleanly.")
            break

        trades = fetch_trades()

        if trades is None:
            consecutive_failures += 1
            backoff = min(POLL_SECONDS * consecutive_failures, 120)
            if consecutive_failures == 3:
                notify("⚠️ Trade monitor: the API has been unreachable "
                       "for a few cycles. Still retrying.")
            time.sleep(backoff)
            continue

        if consecutive_failures >= 3:
            notify("✅ Trade monitor: API reachable again.")
        consecutive_failures = 0

        new_state = {}
        notifications = []

        for t in trades:
            tid = str(t.get("id"))
            if tid == "None":
                continue
            keep = {k: t.get(k) for k in (
                ["id", "trade_id", "symbol", "strike", "option_type",
                 "expiration", "entry_price", "signal_source"] + WATCH_FIELDS)}
            new_state[tid] = keep

            if first_run:
                continue

            if tid not in state:
                notifications.append((t.get("created_at", ""), msg_new_trade(t)))
            else:
                changes = diff_changes(state[tid], t)
                if changes:
                    notifications.append((t.get("updated_at", ""), msg_updated_trade(t, changes)))

        notifications.sort(key=lambda x: x[0])
        for _, text in notifications:
            notify(text)

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
