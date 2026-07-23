#!/usr/bin/env python3
"""
Fetches WFM prices for all tradeable items (no riven auctions).
Writes prices.json to the repo root, committed by GitHub Actions on a schedule.

Output shape per item:
  vwap          – primary price: trimmed-median of 48h buckets (if ≥3 trades), else 90d
  vwap_48h      – trimmed-median of 48-hour bucket medians
  vwap_90d      – trimmed-median of 90-day bucket medians
  volume_48h    – total trades closed in last 48h
  volume_90d    – total trades closed in last 90d
  min_48h       – cheapest closed trade in 48h window
  max_48h       – most expensive closed trade in 48h window
  lowest_ask    – cheapest sell order from an ingame/online user right now
  highest_bid   – highest buy order from an ingame/online user right now
  seller_count  – number of ingame/online sell orders
  buyer_count   – number of ingame/online buy orders
"""

import json
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("Run: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

WFM_BASE    = "https://api.warframe.market"
REQ_SLEEP   = 0.35   # seconds between every request — stays under WFM's 3/sec limit
MIN_VOL_48H = 3      # if fewer trades than this in 48h, fall back to 90d for vwap


# ─── helpers ──────────────────────────────────────────────────────────────────

def trimmed_median(entries: list, trim: float = 0.15) -> int | None:
    """Trimmed median of per-bucket price medians.
    Removes the bottom and top `trim` fraction by price before taking the median.
    This filters single-trade outlier days (e.g. a Disruptor mod at 45 834 p)
    without needing volume data. Falls back to the full-set median when there
    are too few data points to trim.
    """
    prices = sorted(
        e["median"] for e in entries
        if e.get("median") is not None and e["median"] > 0
    )
    if not prices:
        return None
    n   = len(prices)
    cut = int(n * trim)
    lo, hi = cut, n - cut
    slc = prices[lo:hi] if lo < hi else prices
    mid = len(slc) // 2
    if len(slc) % 2 == 0:
        return round((slc[mid - 1] + slc[mid]) / 2)
    return round(slc[mid])


def get(session: requests.Session, url: str, retries: int = 3) -> requests.Response | None:
    """Rate-limited GET. Returns None on 404/403, raises on other errors.
    Retries up to `retries` times on transient connection errors."""
    for attempt in range(1, retries + 1):
        time.sleep(REQ_SLEEP)
        try:
            r = session.get(url, timeout=30)
            if r.status_code in (403, 404):
                return None
            r.raise_for_status()
            return r
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            if attempt == retries:
                raise
            wait = 2 ** attempt  # 2s, 4s
            print(f"  Retry {attempt}/{retries - 1} — {url.split('/')[-2]} ({exc.__class__.__name__}), "
                  f"waiting {wait}s…", file=sys.stderr)
            time.sleep(wait)


# ─── per-item fetches ─────────────────────────────────────────────────────────

def fetch_statistics(session: requests.Session, slug: str) -> dict | None:
    r = get(session, f"{WFM_BASE}/v1/items/{slug}/statistics")
    if r is None:
        return None

    closed = r.json().get("payload", {}).get("statistics_closed", {})
    h48    = closed.get("48hours", [])
    d90    = closed.get("90days",  [])

    vol_48h = sum(e.get("volume", 0) for e in h48)
    vol_90d = sum(e.get("volume", 0) for e in d90)

    # Primary price: trimmed-median of 48h buckets when enough trades, otherwise 90d.
    primary = trimmed_median(h48) if vol_48h >= MIN_VOL_48H else trimmed_median(d90)

    return {
        "vwap":       primary,
        "vwap_48h":   trimmed_median(h48),
        "vwap_90d":   trimmed_median(d90),
        "volume_48h": int(vol_48h),
        "volume_90d": int(vol_90d),
        "min_48h":    min((e["min_price"] for e in h48 if "min_price" in e), default=None),
        "max_48h":    max((e["max_price"] for e in h48 if "max_price" in e), default=None),
    }


def fetch_orders(session: requests.Session, slug: str) -> dict | None:
    r = get(session, f"{WFM_BASE}/v1/items/{slug}/orders")
    if r is None:
        return None

    orders = r.json().get("payload", {}).get("orders", [])
    # Only count users who are actually reachable right now.
    active = [o for o in orders if o.get("user", {}).get("status") in ("ingame", "online")]

    sells = sorted(o["platinum"] for o in active if o.get("order_type") == "sell")
    buys  = sorted(
        (o["platinum"] for o in active if o.get("order_type") == "buy"),
        reverse=True,
    )

    return {
        "lowest_ask":   sells[0] if sells else None,
        "highest_bid":  buys[0]  if buys  else None,
        "seller_count": len(sells),
        "buyer_count":  len(buys),
    }


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "FrameForge-CentralizedPriceCache/1.0 (github.com/WyrmStudios/FrameForgePricing)",
        "Accept":     "application/json",
        "Language":   "en",
    })

    print("Fetching WFM item list…")
    r = session.get(f"{WFM_BASE}/v2/items")
    r.raise_for_status()
    items = r.json()["data"]
    print(f"  {len(items)} items")

    results:  dict = {}
    skipped:  list = []   # 403/404 on statistics — WFM has no data for these
    no_price: list = []   # statistics fetched but vwap is null (no trade history)
    failed:   list = []   # unexpected HTTP error

    for idx, item in enumerate(items, 1):
        slug = item["slug"]
        name = item["i18n"]["en"]["name"]

        if idx % 100 == 0 or idx == len(items):
            pct = idx / len(items) * 100
            print(f"  [{pct:5.1f}%]  {idx}/{len(items)}  {slug}")

        try:
            stats = fetch_statistics(session, slug)
            if stats is None:
                skipped.append(slug)
                continue

            orders = fetch_orders(session, slug)
            results[slug] = {"name": name, **stats, **(orders or {})}

            if stats["vwap"] is None:
                no_price.append(slug)

        except requests.HTTPError as exc:
            print(f"  HTTP {exc.response.status_code} — {slug}", file=sys.stderr)
            failed.append(slug)
        except Exception as exc:
            print(f"  Error — {slug}: {exc}", file=sys.stderr)
            failed.append(slug)

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "item_count":   len(results),
        "with_price":   len(results) - len(no_price),
        "items":        results,
    }

    with open("prices.json", "w", encoding="utf-8") as f:
        json.dump(output, f, separators=(",", ":"))

    size_kb = len(json.dumps(output, separators=(",", ":"))) / 1024

    print(f"\n── Summary ───────────────────────────────")
    print(f"  Total WFM items:      {len(items)}")
    print(f"  Written to JSON:      {len(results)}")
    print(f"    with a price (vwap): {len(results) - len(no_price)}")
    print(f"    no trade history:    {len(no_price)}")
    print(f"  Skipped (403/404):    {len(skipped)}")
    print(f"  Errors:               {len(failed)}")
    print(f"  Output size:          {size_kb:.0f} KB")
    print(f"──────────────────────────────────────────")

    if no_price:
        print(f"\nNo trade history (vwap=null): {no_price[:10]}")
        if len(no_price) > 10:
            print(f"  …and {len(no_price) - 10} more")

    if failed:
        print(f"\nErrors: {failed}", file=sys.stderr)
        threshold = max(5, int(len(items) * 0.01))
        if len(failed) > threshold:
            print(f"Failing: {len(failed)} errors exceeds threshold ({threshold})", file=sys.stderr)
            sys.exit(1)
        print(f"Continuing: {len(failed)} error(s) within acceptable threshold ({threshold})", file=sys.stderr)


if __name__ == "__main__":
    main()
