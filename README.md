# Meta Ads Connector PoC — Skypoint

A working extraction pipeline for Meta Ads campaign performance, plus the
documentation and probes needed to take it into a real Skypoint V2 connector.

**Status (2026-10-27): verified live.** The PoC authenticates with a System User
token, reaches the client ad account through a read-only Partner grant, and pulls
campaign, ad set, ad, creative and daily insights data. See
[Verified results](#verified-results).

## Contents

| File | Purpose |
|---|---|
| `Meta_Ads_Integration.md` | Full integration documentation: connection, auth policy, edge inventory, the restatement finding, proposed production design |
| `meta_client.py` | Shared config, auth policy, rate budget, normalization |
| `meta_edges.py` | Probes all 45 ad-account edges and classifies each as readable or not |
| `meta_incremental.py` | Detects the attribution-restatement problem and implements the fix |
| `meta_extract.py` | Pulls a date window and writes normalized CSV |
| `requirements.txt` | Python dependencies (exact-pinned) |
| `.env.example` | Credential template. Copy to `.env` and fill in |
| `.env` | Local credentials (gitignored, never committed) |

**No credentials are embedded anywhere.** Everything reads from the environment
or a local `.env`. The Meta token is shared across every client, so its blast
radius is the whole platform rather than one client's data.

## Setup

### 1. Meta-side prerequisites

| Item | Value |
|---|---|
| Business Portfolio | Skypoint Cloud Inc. (`1764780201388148`) |
| App | Skypoint Ads Integration (`1728017481639772`) |
| System User | `skypoint-ads-integration` (`61593900157271`) |
| Token permission | `ads_read` only (read-only) |
| Business Verification | **Required.** The portfolio must show *Verified* |

Per client:

1. The client grants Skypoint Cloud Inc. **Partner access** to their ad account
   (*Partial access — View performance* is sufficient).
2. In Business Settings, assign that ad account to the System User with the same
   *View performance* access.
3. Connect the ad account to the app (*Apps → Connect assets → Ad accounts*).

### 2. Generate the token

Business Settings → Users → **System users** → `skypoint-ads-integration` →
**Generate token**:

1. Select the app **Skypoint Ads Integration**.
2. Set expiry to **Never**.
3. Select the **`ads_read`** permission only.
4. Copy the token from the final "Done" screen. **It is shown once.**

Do not tick `ads_management` (write access). `business_management` is optional
and is not needed for any script in this package.

### 3. Configure `.env`

```
META_ACCESS_TOKEN=<system user token>
META_AD_ACCOUNT_ID=<ad account id, digits only>
META_BUSINESS_ID=1764780201388148
META_APP_ID=1728017481639772
META_API_VERSION=v23.0
META_RPS=5
```

### 4. Run

```
pip install -r requirements.txt

python meta_edges.py          # 45-edge inventory
python meta_incremental.py    # incremental strategy: run it TWICE, a day apart
python meta_extract.py        # campaign daily -> output/*.csv
```

### Pulling data with `meta_extract.py`

Output is one CSV per run in `output/`, named
`meta_ads_campaign_daily_<ad_account_id>_<run date>.csv` unless `--out` is given.

**Recent / today's data**

```
python meta_extract.py                  # last 7 days (default), ending yesterday
python meta_extract.py --days 30        # last 30 days
python meta_extract.py --since 2026-10-27 --until 2026-10-27    # a single day (today)
```

Today's row is a **partial day** and will keep changing as the day goes on, so
treat it as provisional. For a stable daily load, pull through yesterday.

**Historical data (backfill)**

```
python meta_extract.py --since 2025-01-01 --until 2026-10-27
python meta_extract.py --since 2026-08-01 --until 2026-08-31    # one month
```

- `--since` and `--until` are `YYYY-MM-DD` and are inclusive.
- Meta retains insights for 37 months, so that is the furthest back you can go.
- A long window is paged automatically (500 rows per page). A pull of about 22
  months took roughly a minute and returned 1,274 rows for the Kyrene account.
- Re-running a window overwrites the file of the same name. A CSV that is open
  in Excel is locked and the write fails with `PermissionError`; close it, or
  pass a different `--out`.

**Choosing the output file**

```
python meta_extract.py --since 2025-01-01 --until 2026-10-27 \
    --out output/meta_ads_campaign_daily_2025-01-01_to_2026-10-27.csv
```

Use `--out` for backfills so they do not collide with the default daily file.

**Which window should I use?**

| Goal | Command |
|---|---|
| Daily refresh | `python meta_extract.py --days 7` |
| Catch up after a gap or restatement | `python meta_extract.py --days 28` (conversions restate for up to 28 days, see below) |
| First-time load / history | `python meta_extract.py --since <start> --until <end> --out <file>` |
| Check the connection works | `python meta_edges.py` (read-only, about 45 calls) |

Conversions for the most recent 28 days are provisional. When refreshing, re-pull
the last 28 days and **replace** (upsert on `date, campaign_id`) rather than
append, or restated conversions will be double-counted.

### Run modes (picked automatically)

| Mode | Triggered by | What it does |
|---|---|---|
| *(refuses)* | no token, or no account id | Nothing runs. Each script names what is missing and exits non-zero |
| `LIVE` | token + account id | Full pulls |

No code changes between modes — the mode follows what is in `.env`.

There is **deliberately no mock or synthetic mode.** Invented rows are
indistinguishable from real ones once they land in a CSV or warehouse table, so a
run started by mistake would contaminate real client data. Every output this
package produces is vendor data, or nothing.

## Verified results

Run against the Kyrene School District ad account (`act_129462520866035`) using a
read-only *View performance* Partner grant.

| Check | Result |
|---|---|
| Authentication | Valid `SYSTEM_USER` token, never expires, scopes `ads_read,public_profile` |
| Ad account node | Kyrene School District, ACTIVE, USD, America/Phoenix, US |
| Object counts | 163 campaigns, 195 ad sets, 213 ads, 1,085 creatives |
| Campaigns | Readable (sample returned, all ACTIVE) |
| Insights | Daily campaign-level rows returned |
| Extract (20–26 Oct 2026) | 27 rows, 80,440 impressions, 3,601 clicks, $1,090.00 spend |

**Conclusion:** *View performance* access is sufficient for the connector's core
reads. A higher permission level is not needed from the client.

Edge inventory (`meta_edges.py`, 45 edges): 20 return data, 11 are reachable but
empty, 4 need query parameters, 10 are not available on this account, and **0 are
permission-denied**.

## What it produces

One row per campaign per day, 14 columns:

```
date, campaign_id, campaign_name, impressions, clicks, spend, ctr, cpc, cpm,
reach, conversions, source, ad_account_id, extracted_at
```

`time_increment=1` is what turns a single date range into daily rows rather than
one rolled-up total for the window.

Two columns are not straight mappings:

- **`conversions` is derived, and it is a business decision.** Meta does not
  return a conversion count. It returns an `actions` array of every action type
  that fired — `link_click`, `landing_page_view`, `post_engagement`,
  `offsite_conversion.fb_pixel_purchase`, and so on. This PoC sums the types
  matching `offsite_conversion`, `purchase`, `lead`, `complete_registration`.
  **That list needs client sign-off before production** — it is the difference
  between two defensible numbers, not between right and wrong.
- **`reach` does not sum.** It is unique people, so daily reach cannot be added
  across days to get weekly reach. A rollup that sums it is wrong.

Instagram placements are part of the same ad account, so they are included in
these rows. The platform split is available by adding a `publisher_platform`
breakdown to the insights request.

## The one thing to read if you read nothing else

Meta's insights data is **not immutable.** Rows for a given date keep changing for
up to 28 days as Meta attributes late conversions back to the click that caused
them. A conversion on the 10th, from an ad clicked on the 3rd, is credited to *the
3rd* and only becomes visible a week later. There is no version token or change
feed; `/insights` is a date-windowed query.

`meta_incremental.py` detects this by fingerprinting every row and reporting which
`(date, campaign_id)` pairs come back with different numbers. Run it twice, a day
apart, to get the first measured result. Expected: spend identical, conversions
moved.

The consequence is a load-semantics rule, not a code detail:

> **Watermark = last complete date. Pull `(watermark − 28 days) .. yesterday`.
> UPSERT on `(date, campaign_id)` — never INSERT.**

An append-only load would double-count every restated conversion. The 28-day
overlap is not waste: it is what corrects restated days, and it is also why a
missed run heals itself with no catch-up branch to write or test.

## Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| `code=200 ... API access blocked` on **every** call, including `/me` | The block is on the app or developer account, not the ad account, so it is not caused by a read-only grant | Check *Meta for Developers* for a restriction or "Access restored" notice, confirm the app is Live, confirm Business Verification is complete. Resolved in this PoC once Meta lifted a temporary developer-account restriction |
| `code=200` or `10` on ad-account calls only, `/me` works | Missing permission on that account | Confirm the Partner grant, System User asset assignment, and `ads_read` on the token |
| `code=190` | Invalid or expired token | Regenerate the System User token. Do not retry in a loop |
| `DENIED ... Partner access` at the ad-account step | The client has not approved the Partner request yet | Wait for the client; re-run `meta_edges.py` to confirm the grant landed |
| Insights return no rows | No delivery in the window | Widen `--days` or check the account is spending |
| `Unknown summary field total_count` on an edge | That edge does not support summary counts | Informational; not a permission issue |

## Platform rules honored in this code

Implemented here rather than just described:

- **Auth failures are never retried** — not on the first call, not mid-pagination.
  The token is static, so nothing a retry could refresh exists, and Meta locks
  Business Managers after repeated bad auth. One retry loop takes down every
  client at once. (`meta_client.MetaAuthError`)
- **Meta returns HTTP 400, not 401, for most OAuth failures** — classification is
  on `error.code`, not status code.
- **Never test with a deliberately wrong token.** Corrupt the scoping field
  (`ad_account_id`) instead; the token is shared across every client.
- **One shared rate budget** (5 req/s), a ceiling independent of concurrency.
- **Field names only in logs, never values.** Tokens are masked even where a
  human needs to confirm which one is loaded.
- **Exact-pinned `requirements.txt`.**
- **No token-refresh path.** It is a static token; there is nothing to refresh,
  and the extra code path would only be somewhere for a bug to hide.

## Decisions already made (do not re-ask)

| Question | Decision |
|---|---|
| **Where the shared credential lives (Key Vault strategy)** | **Option A.** Copy the shared System User token into **each client's own tenant Key Vault at seed time**. No changes to `v2_config_loader.py`'s per-instance `keyVaultSecretName` resolution, so `meta-ads` looks like every other connector at the code level. Agreed with the platform team lead. |
| Cost of that choice | Rotating the token means updating it in every client's vault. Acceptable: Meta has **one** static, non-expiring secret. |
| Data location | Only the credential is shared. Synced data lands **only** in that client's own ADLS Gen2. |
| Token refresh / "publishing the app" / 7-day expiry | **Not applicable to Meta.** The System User token is set to never expire; there is no refresh token and no Testing mode. |
| Token permission | `ads_read` only (read-only). |
| Conversion definition | PoC sums action types matching `offsite_conversion`, `purchase`, `lead`, `complete_registration`. Still **needs client sign-off** before production. |

**Still outstanding (needs a human, not a decision):** the Kyrene sandbox
`instance_id`, `tenant_id`, Key Vault name and ADLS path from Studio. Nothing in
the three-repo build can start for real without them.

## Security notes

- `.env` and `output/` are gitignored. `output/` holds real client data.
- Never paste the token into chat, tickets or commits. If a token is exposed,
  revoke it (System users → **Revoke tokens**) and generate a new one.

## Scope boundaries

CSV output is **PoC-only scaffolding**. The real `source-meta-ads` connector
writes only to the **client's own ADLS Gen2** via `shared_utils.streaming_upload`
— never to local disk, and never to Skypoint-owned storage, even though the
credential doing the fetching is Skypoint's.

`output/` and `state/` are generated, not source.

See `Meta_Ads_Integration.md` for the full picture and the three-repo production
path.
