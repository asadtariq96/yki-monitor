#!/usr/bin/env python3
"""Poll the OPH 'after YKI test' page and alert via Telegram when the
'Is your certificate ready?' section changes."""

import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://www.oph.fi/en/education-and-qualifications/after-yki-test"
START_MARKER = "Is your certificate ready?"
END_MARKER = "We do not share test results"
STATE_FILE = Path(__file__).with_name("state.json")
FAIL_THRESHOLD = 6          # ~30 min of 5-min polls before alerting on an outage
HTTP_TIMEOUT = 30
USER_AGENT = "yki-monitor/1.0 (personal certificate watcher)"

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return False
    text = text[:4000]  # Telegram hard-limits messages to 4096 chars
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": "true"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"ERROR sending telegram: {e}", file=sys.stderr)
        return False


def fetch_section():
    """Return the normalized section text, or None if the section is missing."""
    # This page sits behind a CDN that serves multi-day-stale copies and ignores
    # the origin's no-cache headers. A unique query string forces a fresh origin
    # fetch (verified: x-cache MISS, age 0). ~288 polls/day = negligible load.
    bust = f"_cb={int(time.time())}{random.randint(1000, 9999)}"
    sep = "&" if "?" in URL else "?"
    r = requests.get(
        URL + sep + bust,
        headers={
            "User-Agent": USER_AGENT,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Whole-page visible text, line-normalized: collapse internal whitespace,
    # drop blank lines. Resilient to HTML structure changes around the section.
    lines = []
    for raw in soup.get_text("\n").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line:
            lines.append(line)
    norm = "\n".join(lines)

    i = norm.find(START_MARKER)
    if i == -1:
        return None
    chunk = norm[i:]

    j = chunk.find(END_MARKER)
    if j != -1:
        nl = chunk.find("\n", j)
        chunk = chunk[: nl if nl != -1 else len(chunk)]
    else:
        # End marker gone: capture a bounded window so we don't hash the whole page.
        chunk = "\n".join(chunk.splitlines()[:12])
    return chunk.strip()


def main():
    state = load_state()

    # --- fetch ---
    try:
        section = fetch_section()
    except Exception as e:
        state["fail_count"] = state.get("fail_count", 0) + 1
        print(f"fetch failed ({state['fail_count']}): {e}", file=sys.stderr)
        if state["fail_count"] >= FAIL_THRESHOLD and not state.get("fail_alerted"):
            send_telegram(f"⚠️ YKI monitor: {state['fail_count']} consecutive fetch failures.\nLast error: {e}")
            state["fail_alerted"] = True
        save_state(state)
        return

    # fetch succeeded — clear outage flags
    state["fail_count"] = 0
    state["fail_alerted"] = False

    # --- structure check ---
    if section is None:
        if not state.get("structure_alerted"):
            send_telegram("⚠️ YKI monitor: couldn't find the 'Is your certificate ready?' section. "
                          "The page layout may have changed — check it manually.")
            state["structure_alerted"] = True
        save_state(state)
        return
    state["structure_alerted"] = False

    new_hash = hashlib.sha256(section.encode("utf-8")).hexdigest()

    # --- first run: seed baseline silently (one confirmation message) ---
    if "hash" not in state:
        state["hash"] = new_hash
        state["text"] = section
        save_state(state)
        send_telegram("✅ YKI monitor is running. I'll message you when the certificate section changes.\n\n"
                      "Current text:\n\n" + section)
        return

    # --- change detection ---
    if new_hash != state["hash"]:
        old = state.get("text", "(unknown)")
        send_telegram(
            "\U0001f514 The YKI certificate page changed!\n\n"
            f"{URL}\n\n"
            "----- BEFORE -----\n" + old + "\n\n"
            "----- AFTER -----\n" + section
        )
        state["hash"] = new_hash
        state["text"] = section
        print("change detected and notified", file=sys.stderr)
    else:
        print("no change", file=sys.stderr)
    save_state(state)


if __name__ == "__main__":
    main()
