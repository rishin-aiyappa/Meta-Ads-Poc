"""Demonstrate the incremental extraction strategy for Meta Ads insights.

This is the single most important finding in this PoC.

Meta has no change feed and no version token. /insights is
a date-windowed query, and worse, the rows it returns for a given date KEEP
CHANGING for up to 28 days afterwards as Meta attributes late conversions
back to the click that caused them. A day pulled on Monday is not the same
day pulled on Friday.

So the incremental pattern is:
    watermark  = last complete date successfully loaded
    pull       = (watermark - 28 days) .. yesterday
    load       = UPSERT on (date, campaign_id), never INSERT

This script proves that by pulling a window, storing a fingerprint of every
row, then pulling the same window again and showing which rows came back
with different numbers.

    python meta_incremental.py           # run it twice to see the diff
    python meta_incremental.py --reset   # forget the stored watermark
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

from meta_client import (
    ATTRIBUTION_RESTATEMENT_DAYS,
    NOT_CONFIGURED_HELP,
    MetaAuthError,
    MetaConfig,
    RateLimitBudget,
    banner,
    fetch_insights,
    insights_params,
    load_config,
    mode_line,
    row_key,
)

BUDGET = RateLimitBudget()

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
STATE_PATH = os.path.join(STATE_DIR, "meta_ads_watermark.json")

# How far back the very first run reaches when there is no watermark yet.
# In production this is the backfill horizon agreed with the client; Meta's
# own insights retention is the ceiling (37 months at time of writing).
INITIAL_BACKFILL_DAYS = 14


# --------------------------------------------------------------------------
# Watermark -- the last complete date successfully loaded
# --------------------------------------------------------------------------


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    with open(STATE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def fingerprint(rows: list[dict]) -> dict:
    """Just the volatile metrics, keyed by the merge key. In production this
    is a control table, not a JSON file -- but the shape is the same: one
    row per grain, holding what we last believed to be true."""
    return {
        row_key(r): {
            "spend": r["spend"],
            "conversions": r["conversions"],
            "impressions": r["impressions"],
        }
        for r in rows
    }


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="delete the stored watermark and exit")
    parser.add_argument("--days", type=int, default=INITIAL_BACKFILL_DAYS,
                        help=f"first-run backfill horizon (default {INITIAL_BACKFILL_DAYS})")
    args = parser.parse_args()

    if args.reset:
        if os.path.exists(STATE_PATH):
            os.remove(STATE_PATH)
            print(f"Removed {STATE_PATH}")
        else:
            print("No stored watermark to remove.")
        return 0

    cfg = load_config()
    print("=" * 68)
    print("Meta Ads -- incremental extraction strategy")
    print(f"mode: {mode_line(cfg)}")
    if not cfg.is_configured:
        print()
        print(NOT_CONFIGURED_HELP)
        return 2

    yesterday = date.today() - timedelta(days=1)
    state = load_state()

    # ---- 1) Work out the window from the watermark ------------------------
    banner("1", "Window selection")
    if state:
        watermark = date.fromisoformat(state["last_complete_date"])
        since = watermark - timedelta(days=ATTRIBUTION_RESTATEMENT_DAYS)
        print(f"   stored watermark   : {watermark}")
        print(f"   restatement window : {ATTRIBUTION_RESTATEMENT_DAYS} days")
        print(f"   -> re-pulling from : {since}  (watermark minus the window)")
        print("   The overlap is not waste. It is what makes the load self-healing:")
        print("   a missed run catches up, and a restated day gets corrected.")
    else:
        since = yesterday - timedelta(days=args.days - 1)
        print(f"   no watermark -- first run, backfilling {args.days} days")
        print("   The restatement diff in step 3 needs a stored prior run to")
        print("   compare against, so it appears from the SECOND run onwards.")
    until = yesterday
    print(f"   window             : {since} .. {until}")

    # ---- 2) Pull -----------------------------------------------------------
    banner("2", "Pull  ->  GET /act_<id>/insights")
    params = insights_params(since, until)
    print(f"   level={params['level']}  time_increment={params['time_increment']}  "
          f"limit={params['limit']}")
    print(f"   time_range={params['time_range']}")
    print("   Pagination is cursor-based: Meta returns paging.next as a complete")
    print("   URL with the cursor and token already in it, so following it means")
    print("   sending no params of our own -- ours would clobber the cursor.\n")

    try:
        rows = fetch_insights(cfg, since, until, budget=BUDGET)
    except MetaAuthError as exc:
        print(f"\nFATAL  {exc}")
        return 1
    print(f"   {len(rows)} rows over {(until - since).days + 1} days")

    # ---- 3) Diff against what we last believed -----------------------------
    banner("3", "Diff against the previous run")
    previous = state.get("rows", {})
    current = fingerprint(rows)

    if not previous:
        print("   No previous run to compare against. Run this script again to see")
        print("   which rows come back changed.")
        new_keys, restated, unchanged = list(current), [], []
    else:
        new_keys = [k for k in current if k not in previous]
        restated = [
            k for k in current
            if k in previous and current[k] != previous[k]
        ]
        unchanged = [k for k in current if k in previous and current[k] == previous[k]]

        print(f"   new rows       : {len(new_keys)}")
        print(f"   RESTATED rows  : {len(restated)}   <-- same key, different numbers")
        print(f"   unchanged      : {len(unchanged)}")

        for key in restated[:6]:
            was, now = previous[key], current[key]
            day, campaign = key.split("|")
            print(f"     {day} campaign={campaign}  "
                  f"conversions {was['conversions']} -> {now['conversions']}"
                  f"   spend {was['spend']} -> {now['spend']}")
        if len(restated) > 6:
            print(f"     ... and {len(restated) - 6} more")

        if restated:
            print("\n   This is the finding. Those rows were already loaded once. An")
            print("   append-only load would now hold both versions and double-count")
            print("   every restated conversion. The load MUST upsert on")
            print("   (date, campaign_id) -- the merge key meta_client.row_key builds.")

    # ---- 4) Advance the watermark ------------------------------------------
    banner("4", "Advance the watermark")
    new_state = {
        "last_run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_complete_date": until.isoformat(),
        "row_count": len(rows),
        "rows": current,
    }
    save_state(new_state)
    print(f"   last_complete_date = {until}")
    print(f"   written to {os.path.relpath(STATE_PATH)}")
    print("   In production this is one row in a control table holding the")
    print("   last complete date loaded.")

    print("=" * 68)
    print("DONE  -- run again to see the restatement diff grow.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
