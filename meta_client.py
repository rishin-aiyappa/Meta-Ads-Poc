"""Shared Meta Marketing API client for every script in this package.

Why one shared module instead of self-contained scripts: the auth and
rate-limit rules below have shared blast radius. Skypoint holds ONE System User token covering every client, so a
retry loop on a bad credential does not lock out one client -- it locks out
all of them at once. Those rules live in exactly one place here rather than
being copy-pasted into four scripts where they can quietly drift apart.

Credentials are read from the environment (or a local .env) and never
embedded in source -- see .env.example.

Docs: https://developers.facebook.com/docs/marketing-api/insights
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator

import requests

# .env support is a convenience, not a requirement -- the scripts run fine on
# plain environment variables if python-dotenv isn't installed.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - optional dependency
    pass


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Confirm this against Meta's current release before the real build -- Meta
# ships a new Graph API version roughly quarterly and deprecates each one
# about two years after release. Overridable with META_API_VERSION.
DEFAULT_API_VERSION = "v23.0"

# Hard ceiling shared by every call in the process, matching the platform's
# existing connectors (source-paychex uses the same shape). More concurrency
# means more threads competing for this ceiling, never a way to exceed it.
DEFAULT_RPS = 5.0

GRAPH_HOST = "https://graph.facebook.com"

# Insights numbers are not final when first published. Meta keeps attributing
# conversions to a click/view for the length of the attribution window, so a
# date's row keeps changing after the fact. 28 days is the widest standard
# window; re-pulling that far back is what makes the extract self-healing.
ATTRIBUTION_RESTATEMENT_DAYS = 28


@dataclass(frozen=True)
class MetaConfig:
    """Everything needed to talk to the Marketing API for one ad account."""

    access_token: str
    ad_account_id: str
    api_version: str = DEFAULT_API_VERSION
    requests_per_second: float = DEFAULT_RPS
    # Non-secret identifiers. Optional.
    business_id: str = ""
    app_id: str = ""

    @property
    def has_token(self) -> bool:
        """A token alone is enough to ask what this token can reach -- which
        is the whole question during onboarding, before any client has
        approved Partner access."""
        return bool(self.access_token)

    @property
    def is_configured(self) -> bool:
        """Enough to pull data: a token AND an account to point it at."""
        return bool(self.access_token and self.ad_account_id)

    @property
    def account_path(self) -> str:
        """Meta wants the ad account as act_<id>; tolerate either input form.
        Unset renders as a placeholder so narrated output reads as the call
        that would be made rather than a malformed path."""
        acct = self.ad_account_id or "<AD_ACCOUNT_ID>"
        return acct if acct.startswith("act_") else f"act_{acct}"


def load_config() -> MetaConfig:
    """Reads config from the environment. Missing credentials are not an
    error here -- load_config always succeeds, and the data paths refuse to
    run via require_live(). Nothing is ever fabricated to fill a gap."""
    return MetaConfig(
        access_token=os.getenv("META_ACCESS_TOKEN", "").strip(),
        ad_account_id=os.getenv("META_AD_ACCOUNT_ID", "").strip(),
        api_version=os.getenv("META_API_VERSION", DEFAULT_API_VERSION).strip(),
        requests_per_second=float(os.getenv("META_RPS", DEFAULT_RPS)),
        business_id=os.getenv("META_BUSINESS_ID", "").strip(),
        app_id=os.getenv("META_APP_ID", "").strip(),
    )


def mask(secret: str) -> str:
    """Tokens never reach a log in full. The platform's logging standard is
    field names only, never values -- this exists for the few places where a
    human genuinely needs to confirm *which* token is loaded."""
    if not secret:
        return "<unset>"
    return f"{secret[:4]}...{secret[-4:]} (len={len(secret)})"


# --------------------------------------------------------------------------
# Errors -- the distinction between these drives the retry policy
# --------------------------------------------------------------------------


class MetaError(RuntimeError):
    """Base for every error this client raises."""


class MetaNotConfiguredError(MetaError):
    """Credentials are missing, so there is nothing to pull.

    This package deliberately has NO mock/synthetic mode. An earlier revision
    did, and it was removed: synthetic rows are indistinguishable from real
    ones once they are in a CSV or a warehouse table, and a run started by
    mistake would quietly contaminate a client's data with invented numbers.

    Refusing to run is the safe failure. Every output this package produces is
    real vendor data or it is nothing.
    """


class MetaAuthError(MetaError):
    """The shared System User token is bad, expired, or revoked.

    NEVER retried. Meta throttles and eventually locks a Business Manager
    after repeated failed auth, and this token is shared across every client
    -- one retry loop takes down every Meta Ads instance on the platform.
    Fail immediately, surface it, let a human fix the token.
    """


class MetaPermissionError(MetaError):
    """Token is valid but lacks the permission (or the object is invisible).

    Not retried -- the answer will not change on a second attempt.
    """


class MetaRateLimitError(MetaError):
    """Meta is throttling. Retried with backoff, honoring Meta's own
    estimated_time_to_regain_access when it gives us one."""


class MetaAPIError(MetaError):
    """Anything else the Graph API rejected."""


# Meta returns HTTP 400 (not 401) for most OAuth failures, with the real
# reason in error.code -- so status code alone is not enough to classify.
AUTH_CODES = {102, 190, 463, 467}
PERMISSION_CODES = {3, 10, 200, 272, 294, 299}
RATE_LIMIT_CODES = {4, 17, 32, 613, 80000, 80001, 80002, 80003, 80004}
TRANSIENT_CODES = {1, 2}
# code 100 / subcode 33: object doesn't exist, isn't visible to this token, or
# the edge doesn't support this operation. Used heavily by meta_edges.py.
NOT_AVAILABLE_SUBCODES = {33}


def classify_error(status: int, body: dict) -> tuple[str, str]:
    """Maps a Graph API error onto (kind, human message).

    kind is one of: auth, permission, rate_limit, transient, not_available,
    bad_request, http.
    """
    err = (body or {}).get("error") or {}
    code = err.get("code")
    subcode = err.get("error_subcode")
    msg = err.get("message", "").strip() or f"HTTP {status}"
    detail = f"code={code} subcode={subcode}: {msg}"

    if code in AUTH_CODES or err.get("type") == "OAuthException" and code in AUTH_CODES:
        return "auth", detail
    if code in RATE_LIMIT_CODES:
        return "rate_limit", detail
    if code in PERMISSION_CODES:
        if "API access blocked" in msg:
            detail += (" [app/developer-account level, not the ad-account grant:"
                       " check Meta for Developers for a restriction, that the"
                       " app is Live, and Business Verification]")
        return "permission", detail
    if code == 100 and subcode in NOT_AVAILABLE_SUBCODES:
        return "not_available", detail
    if code == 100:
        return "bad_request", detail
    if code in TRANSIENT_CODES:
        return "transient", detail
    if status >= 500:
        return "transient", detail
    if status == 401 or status == 403:
        # Defensive: an OAuth failure that didn't carry a recognized code.
        return "auth", detail
    return "http", detail


# --------------------------------------------------------------------------
# Rate limiting -- one shared budget, separate from concurrency
# --------------------------------------------------------------------------


class RateLimitBudget:
    """A hard ceiling on requests per second, shared by every caller.

    Deliberately a ceiling and not a per-thread allowance: the platform's
    existing connectors run up to 10 workers against one budget, and the
    budget is what keeps the vendor happy regardless of worker count.
    """

    def __init__(self, requests_per_second: float = DEFAULT_RPS) -> None:
        self._interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed:
                time.sleep(self._next_allowed - now)
                now = self._next_allowed
            self._next_allowed = now + self._interval


def parse_usage_headers(headers) -> dict:
    """Meta reports how much of the rate budget is spent in response headers.
    Worth logging in production -- it's the early warning before a 613."""
    out = {}
    for name in ("x-app-usage", "x-ad-account-usage", "x-business-use-case-usage"):
        raw = headers.get(name)
        if not raw:
            continue
        try:
            out[name] = json.loads(raw)
        except (ValueError, TypeError):
            out[name] = raw
    return out


def _estimated_backoff(usage: dict) -> float | None:
    """Meta sometimes tells us exactly how long to wait, in minutes."""
    buc = usage.get("x-business-use-case-usage")
    if not isinstance(buc, dict):
        return None
    for entries in buc.values():
        for entry in entries if isinstance(entries, list) else []:
            minutes = entry.get("estimated_time_to_regain_access")
            if minutes:
                return float(minutes) * 60.0
    return None


# --------------------------------------------------------------------------
# Logging -- structured, field names only
# --------------------------------------------------------------------------


def log(event: str, **fields: Any) -> None:
    """Deliberately tiny. The real connector uses the platform's logging
    standard; this exists so the POC's output is greppable and so that no
    script is tempted to print a token."""
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    parts = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    print(f"  [{stamp}] {event} {parts}".rstrip())


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

MAX_RETRY_ATTEMPTS = 4


def request_with_retry(
    method: str,
    url: str,
    *,
    budget: RateLimitBudget,
    params: dict | None = None,
    timeout: int = 60,
    is_first_auth_call: bool = False,
    verbose: bool = False,
) -> requests.Response:
    """One Graph API call, with the retry policy the platform requires.

    The rule that matters: auth failures are NEVER retried, whether they hit
    on the first call or deep inside a paginated pull. Meta's token here is
    static -- there is no refresh flow, so "expired, refresh and retry" is
    not a thing that can succeed. A 401/190 on the first call means the
    shared token is dead; the same error later means it was revoked
    mid-pull. Both need a human, and both affect every client at once.

    Rate limits and genuine 5xx blips are the only retryable cases.
    """
    attempt = 0
    while True:
        attempt += 1
        budget.acquire()
        try:
            resp = requests.request(method, url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            if attempt >= MAX_RETRY_ATTEMPTS:
                raise MetaAPIError(f"network error after {attempt} attempts: {exc}") from exc
            delay = _backoff_delay(attempt)
            log("network_retry", attempt=attempt, sleep_s=round(delay, 1))
            time.sleep(delay)
            continue

        usage = parse_usage_headers(resp.headers)
        if verbose and usage:
            log("rate_usage", **{k.replace("-", "_"): json.dumps(v) for k, v in usage.items()})

        if resp.ok:
            return resp

        try:
            body = resp.json()
        except ValueError:
            body = {}
        kind, detail = classify_error(resp.status_code, body)

        if kind == "auth":
            where = "first call" if is_first_auth_call else "mid-pull"
            raise MetaAuthError(
                f"Shared System User token rejected on the {where} -- {detail}. "
                "NOT retrying: this token is shared across every client and Meta "
                "locks accounts on repeated bad auth. A human needs to re-mint it "
                "in the Business Manager."
            )
        if kind == "permission":
            raise MetaPermissionError(detail)
        if kind in ("bad_request", "not_available", "http"):
            raise MetaAPIError(detail)

        # Only rate_limit and transient reach here -- the two retryable kinds.
        if attempt >= MAX_RETRY_ATTEMPTS:
            cls = MetaRateLimitError if kind == "rate_limit" else MetaAPIError
            raise cls(f"{detail} (gave up after {attempt} attempts)")

        delay = _estimated_backoff(usage) if kind == "rate_limit" else None
        delay = delay if delay else _backoff_delay(attempt)
        log(f"{kind}_retry", attempt=attempt, sleep_s=round(delay, 1))
        time.sleep(delay)


def _backoff_delay(attempt: int) -> float:
    """Exponential with jitter, so parallel workers don't resynchronize."""
    return min(2.0 ** attempt, 60.0) + random.uniform(0, 1.0)


def graph_get(
    cfg: MetaConfig,
    path: str,
    params: dict | None = None,
    *,
    budget: RateLimitBudget,
    is_first_auth_call: bool = False,
    verbose: bool = False,
) -> dict:
    """GET one Graph API node/edge and return the decoded body."""
    url = f"{GRAPH_HOST}/{cfg.api_version}/{path.lstrip('/')}"
    merged = dict(params or {})
    merged["access_token"] = cfg.access_token
    resp = request_with_retry(
        "GET",
        url,
        params=merged,
        budget=budget,
        is_first_auth_call=is_first_auth_call,
        verbose=verbose,
    )
    return resp.json()


def paginate(
    cfg: MetaConfig,
    path: str,
    params: dict | None = None,
    *,
    budget: RateLimitBudget,
    max_pages: int = 100,
    verbose: bool = False,
) -> Iterator[tuple[int, list[dict]]]:
    """Walks a cursor-paginated edge, yielding (page_number, rows).

    Meta hands back paging.next as a fully-formed URL with the cursor and the
    access token already baked in, so following it means passing params=None
    -- re-sending our own params would clobber the cursor.
    """
    url = f"{GRAPH_HOST}/{cfg.api_version}/{path.lstrip('/')}"
    merged = dict(params or {})
    merged["access_token"] = cfg.access_token
    req_params: dict | None = merged
    page = 0
    first = True

    while url and page < max_pages:
        page += 1
        resp = request_with_retry(
            "GET",
            url,
            params=req_params,
            budget=budget,
            is_first_auth_call=first,
            verbose=verbose,
        )
        first = False
        payload = resp.json()
        yield page, payload.get("data", [])

        url = (payload.get("paging") or {}).get("next")
        req_params = None  # the next URL already carries cursor + token


# --------------------------------------------------------------------------
# Normalization -- the one shared row shape everything in this POC produces
# --------------------------------------------------------------------------

COLUMNS = [
    "date",
    "campaign_id",
    "campaign_name",
    "impressions",
    "clicks",
    "spend",
    "ctr",
    "cpc",
    "cpm",
    "reach",
    "conversions",
    "source",
    # POC metadata.
    "ad_account_id",
    "extracted_at",
]

INSIGHTS_FIELDS = [
    "campaign_id",
    "campaign_name",
    "spend",
    "impressions",
    "clicks",
    "ctr",
    "cpc",
    "cpm",
    "reach",
    "actions",  # nested list of {action_type, value}; conversions live in here
]

# Meta does not hand back a single "conversions" number. It hands back every
# action type that fired, and you decide which ones count.
CONVERSION_ACTION_TYPES = (
    "offsite_conversion",
    "purchase",
    "lead",
    "complete_registration",
)


def extract_conversions(actions: list | None) -> float:
    """Sums the action types that represent an actual conversion event."""
    if not actions:
        return 0.0
    total = 0.0
    for action in actions:
        action_type = action.get("action_type", "")
        if any(ct in action_type for ct in CONVERSION_ACTION_TYPES):
            total += _as_float(action.get("value"))
    return round(total, 2)


def _as_float(value: Any) -> float:
    """Meta returns every numeric metric as a string, and omits a field
    entirely when it's zero -- so both None and "" have to survive this."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    return int(_as_float(value))


def normalize_insight(item: dict, cfg: MetaConfig, extracted_at: str) -> dict:
    """One raw Graph API insights row -> one row of the shared schema."""
    return {
        "date": item.get("date_start"),
        "campaign_id": item.get("campaign_id"),
        "campaign_name": item.get("campaign_name"),
        "impressions": _as_int(item.get("impressions")),
        "clicks": _as_int(item.get("clicks")),
        "spend": round(_as_float(item.get("spend")), 2),
        "ctr": round(_as_float(item.get("ctr")), 4),
        "cpc": round(_as_float(item.get("cpc")), 4),
        "cpm": round(_as_float(item.get("cpm")), 4),
        "reach": _as_int(item.get("reach")),
        "conversions": extract_conversions(item.get("actions")),
        "source": "meta",
        "ad_account_id": cfg.ad_account_id,
        "extracted_at": extracted_at,
    }


def insights_params(since: date, until: date, level: str = "campaign") -> dict:
    """Builds the /insights query. time_increment=1 is what turns a single
    range into one row per campaign per day rather than one rolled-up total."""
    return {
        "level": level,
        "fields": ",".join(INSIGHTS_FIELDS),
        "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
        "time_increment": 1,
        "limit": 500,
    }


# --------------------------------------------------------------------------
# Shared script scaffolding
# --------------------------------------------------------------------------


NOT_CONFIGURED_HELP = """\
Not configured -- nothing to pull, and this package will not invent anything.

Set these in .env (copy .env.example):
    META_ACCESS_TOKEN       the shared System User token
    META_AD_ACCOUNT_ID      the client's ad account id (digits, act_ optional)

There is deliberately no mock/synthetic mode. Invented rows are
indistinguishable from real ones once written, so a run started by mistake
would contaminate real data. This package emits vendor data or nothing."""


def require_live(cfg: MetaConfig) -> None:
    """Gate every data path. Raises rather than returning anything fabricated."""
    if not cfg.is_configured:
        raise MetaNotConfiguredError(NOT_CONFIGURED_HELP)


def banner(step: str, title: str) -> None:
    print("=" * 68)
    print(f"{step}) {title}")


def mode_line(cfg: MetaConfig) -> str:
    if cfg.is_configured:
        return (
            f"LIVE  -- account={cfg.account_path} api={cfg.api_version} "
            f"token={mask(cfg.access_token)} ceiling={cfg.requests_per_second}/s"
        )
    if cfg.has_token:
        return (
            f"DISCOVERY  -- token present, META_AD_ACCOUNT_ID not set. "
            f"token={mask(cfg.access_token)} api={cfg.api_version}. "
            "Can ask what this token reaches; cannot pull data yet."
        )
    return "NOT CONFIGURED  -- META_ACCESS_TOKEN not set. No data path will run."


def default_window(days: int = 7) -> tuple[date, date]:
    """Yesterday backwards. Today is deliberately excluded -- a partial day's
    insights row is guaranteed to be restated and just adds noise."""
    until = date.today() - timedelta(days=1)
    since = until - timedelta(days=days - 1)
    return since, until


def fetch_insights(
    cfg: MetaConfig,
    since: date,
    until: date,
    *,
    budget: RateLimitBudget,
    level: str = "campaign",
    verbose: bool = False,
) -> list[dict]:
    """One normalized row per campaign per day across the window.

    Pages /insights until Meta stops handing back a next cursor. Raises
    MetaNotConfiguredError rather than returning anything when credentials are
    missing -- every row this returns came from Meta.
    """
    require_live(cfg)
    extracted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    rows: list[dict] = []
    for page, raw_rows in paginate(
        cfg,
        f"{cfg.account_path}/insights",
        insights_params(since, until, level),
        budget=budget,
        verbose=verbose,
    ):
        log("insights_page", page=page, rows=len(raw_rows))
        rows.extend(normalize_insight(raw, cfg, extracted_at) for raw in raw_rows)
    return rows


def row_key(row: dict) -> str:
    """The natural key of a normalized row.

    This is the merge key in Bronze. It exists because Meta rows are not
    append-only -- the same (date, campaign) is re-issued with different
    numbers as attribution matures, so a warehouse load has to upsert on
    this, never insert.
    """
    return f"{row['date']}|{row['campaign_id']}"
