"""Probe which Marketing API edges ('tables') are reachable for this token.

Meta has no listable table catalog: the Graph API exposes a fixed set of edges
hanging off the ad account node, and the only way to know which ones this token
can actually read is to ask each one.

Every edge is hit with limit=1 -- enough to classify it, cheap enough that
the whole sweep stays inside the rate budget.

    python meta_edges.py
    python meta_edges.py --json    # machine-readable summary
"""
from __future__ import annotations

import argparse
import json
import sys

from meta_client import (
    MetaAPIError,
    MetaAuthError,
    MetaConfig,
    MetaError,
    MetaPermissionError,
    RateLimitBudget,
    default_window,
    graph_get,
    insights_params,
    load_config,
    mode_line,
)

BUDGET = RateLimitBudget()

# Every ad-account edge worth knowing about, with the permission it needs and
# whether we expect this token to reach it. Without credentials the catalogue
# is printed as a checklist; a live run replaces it with what actually happened.
#   perm:  the Marketing API permission required
#   note:  why it matters, or why it doesn't
EDGES: list[tuple[str, str, str]] = [
    # --- the core reporting surface -------------------------------------
    ("campaigns",              "ads_read", "Campaign objects: name, objective, status, budget"),
    ("adsets",                 "ads_read", "Ad sets: targeting, schedule, bid strategy"),
    ("ads",                    "ads_read", "Individual ads and their creative links"),
    ("insights",               "ads_read", "THE metrics edge -- spend/impressions/clicks/actions"),
    ("adcreatives",            "ads_read", "Creative bodies, images, links, CTAs"),
    ("adimages",               "ads_read", "Image assets referenced by creatives"),
    ("advideos",               "ads_read", "Video assets referenced by creatives"),
    ("adlabels",               "ads_read", "Labels used to group campaigns/ad sets/ads"),
    # --- audiences and conversion tracking ------------------------------
    ("customaudiences",        "ads_read", "Custom + lookalike audience definitions"),
    ("saved_audiences",        "ads_read", "Saved targeting definitions"),
    ("customconversions",      "ads_read", "Custom conversion definitions (pixel rules)"),
    ("adspixels",              "ads_read", "Meta pixels attached to this account"),
    ("product_audiences",      "ads_read", "Catalog-driven dynamic audiences"),
    # --- account administration -----------------------------------------
    ("activities",             "ads_read", "Change log: who changed what, when"),
    ("assigned_users",         "business_management", "Users/system users with access"),
    ("agencies",               "ads_management", "Partner businesses with access"),
    ("applications",           "ads_management", "Apps linked to the account"),
    ("promote_pages",          "ads_management", "Pages promotable from this account"),
    ("subscribed_apps",        "ads_management", "Webhook subscriptions"),
    ("publisher_block_lists",  "ads_management", "Placement block lists"),
    ("adrules_library",        "ads_management", "Automated rules"),
    ("connected_instagram_accounts", "ads_management", "Linked IG accounts"),
    ("ad_place_page_sets",     "ads_management", "Place page sets for local ads"),
    ("partnerintegrations",    "ads_management", "Partner integrations (CAPI etc.)"),
    ("ios_fourteen_campaign_limits", "ads_read", "SKAdNetwork campaign limits"),
    ("dsa_recommendations",    "ads_read", "EU Digital Services Act recommendations"),
    ("impacting_ad_studies",   "ads_read", "Lift studies touching this account"),
    # --- async reporting -------------------------------------------------
    ("async_requests",         "ads_read", "Async job handles"),
    ("asyncadrequestsets",     "ads_management", "Bulk async ad creation sets"),
    # --- estimation / lookup: these need query params, not a bare read ---
    ("delivery_estimate",      "ads_read", "Requires targeting_spec"),
    ("reachestimate",          "ads_read", "Requires targeting_spec"),
    ("targetingsearch",        "ads_read", "Requires q= search term"),
    ("targetingbrowse",        "ads_read", "Targeting taxonomy browse"),
    ("generatepreviews",       "ads_read", "Requires creative + ad_format"),
    ("minimum_budgets",        "ads_read", "Minimum budget by currency"),
    ("max_bid",                "ads_management", "Account max bid"),
    ("tracking",               "ads_management", "Account-level tracking specs"),
    # --- deprecated / historical: probed so the report is complete -------
    ("users",                  "ads_management", "DEPRECATED -> use assigned_users"),
    ("instagram_accounts",     "ads_management", "DEPRECATED in recent versions"),
    ("offsitepixels",          "ads_management", "DEPRECATED -> adspixels"),
    ("broadtargetingcategories", "ads_read", "DEPRECATED targeting taxonomy"),
    ("matched_search_applications", "ads_management", "DEPRECATED"),
    ("businessprojects",       "business_management", "DEPRECATED"),
    ("deprecatedtargetingadsets", "ads_read", "Ad sets using retired targeting"),
    ("customaudiencestos",     "ads_management", "Custom Audience ToS status"),
]

# Edges that will not answer a bare limit=1 read -- they need a query. Their
# 'bad request' response is a pass, not a failure: it proves the edge exists
# and the token reaches it.
NEEDS_PARAMS = {
    "delivery_estimate", "reachestimate", "targetingsearch", "targetingbrowse",
    "generatepreviews", "customaudiencestos",
}

# Edges whose read needs a date range rather than a bare list.
NEEDS_DATE = {"insights"}


def probe(cfg: MetaConfig, edge: str) -> tuple[str, str]:
    """Returns (status, detail). Status is one of:
    data / empty / needs-params / denied / unavailable."""
    params: dict = {"limit": 1}
    if edge in NEEDS_DATE:
        since, until = default_window(days=7)
        params = {**insights_params(since, until), "limit": 1}

    try:
        body = graph_get(cfg, f"{cfg.account_path}/{edge}", params, budget=BUDGET)
    except MetaPermissionError as exc:
        return "denied", str(exc)[:60]
    except MetaAPIError as exc:
        msg = str(exc)[:60]
        if edge in NEEDS_PARAMS:
            # Rejected for missing arguments, which means it's there.
            return "needs-params", "reachable; requires query params"
        return "unavailable", msg
    except MetaError as exc:
        return "unavailable", str(exc)[:60]

    # Some nodes return a plain object rather than a {data: [...]} list.
    if "data" in body:
        rows = body.get("data", [])
        n = len(rows)
        if n == 0:
            return "empty", "0 rows"
        more = " +more" if (body.get("paging") or {}).get("next") else ""
        return "data", f"{n}{more}"
    return "data", "single object"


def run_live(cfg: MetaConfig) -> dict:
    buckets: dict[str, list] = {
        "data": [], "empty": [], "needs-params": [], "denied": [], "unavailable": []
    }
    print(f"{'Edge':32s} {'status':13s} detail")
    print("-" * 78)
    for edge, perm, _note in EDGES:
        status, detail = probe(cfg, edge)
        buckets[status].append(edge)
        print(f"{edge:32s} {status:13s} {detail}")
    return buckets


def run_catalogue(cfg: MetaConfig) -> dict:
    """No vendor calls. Prints the inventory with the permission each edge
    needs, so the pre-credential value of this script is the checklist of
    what to confirm the moment the token lands."""
    print(f"{'Edge':32s} {'permission':20s} note")
    print("-" * 92)
    for edge, perm, note in EDGES:
        tag = "  [needs params]" if edge in NEEDS_PARAMS else ""
        print(f"{edge:32s} {perm:20s} {note}{tag}")
    return {"pending": [e for e, _, _ in EDGES]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args()

    cfg = load_config()
    print(f"Meta Marketing API -- edge inventory")
    print(f"mode: {mode_line(cfg)}\n")

    try:
        buckets = run_live(cfg) if cfg.is_configured else run_catalogue(cfg)
    except MetaAuthError as exc:
        print(f"\nFATAL  {exc}")
        return 1

    print("-" * 78)
    if cfg.is_configured:
        print(f"\nAccessible with data   : {len(buckets['data'])}")
        print(f"Accessible but empty   : {len(buckets['empty'])}")
        print(f"Reachable, needs params: {len(buckets['needs-params'])}")
        print(f"Permission denied      : {len(buckets['denied'])}")
        print(f"Not available          : {len(buckets['unavailable'])}")
    else:
        print(f"\n{len(EDGES)} edges catalogued, 0 probed -- no credentials, so no vendor")
        print("calls were made and no data was produced.")
        print("Re-run with META_ACCESS_TOKEN + META_AD_ACCOUNT_ID set to classify them.")
    print(f"Total edges            : {len(EDGES)}")

    if args.json:
        print("\n" + json.dumps(buckets, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
