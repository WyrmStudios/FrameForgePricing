#!/usr/bin/env python3
"""Compare WFCD item files against the last snapshot and announce new items on Discord.

Usage: wfcd_new_items.py <raw_dir> <snapshot_path>

Env:
  DISCORD_WEBHOOK_URL  required unless DRY_RUN=1
  DRY_RUN=1            print the messages instead of posting, and leave the snapshot alone

The first run (no snapshot yet) only records a baseline and announces nothing.
The snapshot is rewritten only after every Discord post has succeeded, so a failed
post is retried on the next run.
"""
import glob
import json
import os
import sys
import time
import urllib.request

MAX_MESSAGE_CHARS = 1900  # Discord's limit is 2000; keep a margin
MAX_LOSS_RATIO = 0.9      # refuse to shrink the snapshot this much (guards against partial downloads)
POST_INTERVAL_S = 1.0     # stay well inside Discord's webhook rate limit


def load_current(raw_dir):
    items = {}
    for path in sorted(glob.glob(os.path.join(raw_dir, "*.json"))):
        with open(path, encoding="utf-8") as f:
            for item in json.load(f):
                unique = item.get("uniqueName")
                if unique:
                    items[unique] = item.get("name") or unique
    return items


def load_snapshot(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return set(json.load(f))


def write_snapshot(path, unique_names):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(unique_names), f, separators=(",", ":"))


def build_messages(new_items):
    header = f"🆕 **{len(new_items)} new WFCD item(s)**"
    messages = []
    current = header
    for unique, name in new_items:
        line = f"• {name} — `{unique}`"
        if len(current) + 1 + len(line) > MAX_MESSAGE_CHARS:
            messages.append(current)
            current = line
        else:
            current += "\n" + line
    messages.append(current)
    return messages


def post(webhook, content):
    body = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "FrameForgePricing-wfcd-check"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def main():
    raw_dir, snapshot_path = sys.argv[1], sys.argv[2]
    dry_run = os.environ.get("DRY_RUN") == "1"

    current = load_current(raw_dir)
    if not current:
        print("ERROR: no items found in", raw_dir)
        return 1

    previous = load_snapshot(snapshot_path)
    if previous is None:
        if not dry_run:
            write_snapshot(snapshot_path, current.keys())
        print(f"Baseline snapshot created with {len(current)} items; nothing announced.")
        return 0

    if previous and len(current) < len(previous) * MAX_LOSS_RATIO:
        print(f"ERROR: {len(current)} items vs {len(previous)} in snapshot; refusing to overwrite.")
        return 1

    new_items = sorted(
        ((u, n) for u, n in current.items() if u not in previous),
        key=lambda pair: pair[1].lower(),
    )
    print(f"{len(new_items)} new item(s) since last snapshot.")

    if new_items:
        webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if not dry_run and not webhook:
            print("ERROR: DISCORD_WEBHOOK_URL is not set.")
            return 1
        for msg in build_messages(new_items):
            if dry_run:
                print(msg)
                print("-----")
            else:
                post(webhook, msg)
                time.sleep(POST_INTERVAL_S)

    if not dry_run and set(current) != previous:
        write_snapshot(snapshot_path, current.keys())
        print("Snapshot updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
