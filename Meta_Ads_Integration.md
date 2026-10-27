# Meta Ads Integration — Skypoint Ads Connector

**Status:** PoC **verified live (2026-10-27)** against the Kyrene School District
ad account using a read-only Partner grant. Authentication, account lookup,
object counts, campaign reads and daily insights all work.
**Written:** 2026-09-04 · **Updated:** 2026-10-27
**Owner:** Rishin Aiyappa (Skypoint)
**Access level:** `ads_read` (read-only) via a shared System User token

### Meta account setup — complete

| Item | Value |
|------|-------|
| Business Portfolio | Skypoint Cloud Inc. — `1764780201388148` |
| App | Skypoint Ads Integration — `1728017481639772` |
| System User | `skypoint-ads-integration` — `61593900157271` |
| Token scope | `ads_read` (read-only), no expiry |
| Business Verification | Verified (2026-10-26) |
| Token location | Local `.env` only; it appears in no file in this repo. |

Those IDs are non-secret identifiers. **The token is the only secret** and is
never committed, logged in full, or pasted into a message. If a token is exposed,
revoke it (System users → Revoke tokens) and generate a new one.

### First client: Kyrene School District

| Item | Value |
|------|-------|
| Ad account | Kyrene School District — `act_129462520866035` |
| Grant | Partner access, *Partial access (View performance)* — read-only |
| System User asset assignment | Same *View performance* access |
| App connection | Ad account connected to Skypoint Ads Integration (Business Settings → Apps → Connect assets) |
| Account facts | ACTIVE, USD, America/Phoenix, country US |

---

## 1. Overview

This integration connects Skypoint to the **Meta Marketing API** to pull
campaign-level daily advertising performance for agency clients. Meta exposes a
JSON-over-HTTPS Graph API with a fixed set of edges hanging off an ad account
node — campaigns, ad sets, ads, creatives, audiences, and the one that matters
most, `insights`.

```
Skypoint                                     Meta cloud
┌────────────────────────────┐              ┌─────────────────────────────┐
│ ONE System User token      │  bearer      │ graph.facebook.com          │
│ minted in Skypoint's       ├─────────────►│  /act_<ad_account_id>/...   │
│ Business Manager           │  (static)    │                             │
│ scope: ads_read            │              │ Which client's data returns  │
└────────────┬───────────────┘              │ is decided by the ID in the  │
             │                              │ URL, not by a second token.  │
             │  HTTPS GET                   └─────────────────────────────┘
             │    /act_<id>/campaigns    (reference objects)
             │    /act_<id>/insights     (the metrics, date-windowed)
             ▼
Normalized rows → local CSV (this PoC)
                → the CLIENT'S OWN ADLS Gen2 (production, via SkyConnect)
```

**The structural difference from every other connector on the platform:** there
is no per-client secret. Skypoint owns one token; the client's only action is
approving Partner access. `ad_account_id` is a *scoping input*, not a credential
— it is the only thing that changes per client, and it travels in the URL.

No SDK is required — plain HTTPS GETs with `requests` work, which is what this
PoC does.

---

## 2. Connection details

| Item | Value |
|------|-------|
| Host | `https://graph.facebook.com` |
| API version | `v23.0` (configurable via `META_API_VERSION`) — **confirm against Meta's current release**; versions are deprecated ~2 years after release |
| Endpoint shape | `GET /<version>/act_<ad_account_id>/insights` |
| Credential | One System User access token, `ads_read`, minted once in Skypoint's Business Manager |
| Credential lifetime | **Static — no refresh flow.** A System User token does not expire; it is either valid or revoked |
| Per-client input | `ad_account_id` (scoping input, `isCredential: false`) |
| Rate ceiling | 5 requests/second in this PoC (`META_RPS`), shared across all callers |
| API reference | https://developers.facebook.com/docs/marketing-api/insights |

### Credential handling

Nothing in this package hardcodes a secret. All configuration is read from the
environment or a local `.env`, which is gitignored. This matters most for a
credential whose blast radius is every client at once.

### Authentication flow

1. ~~**One-time, platform-level:** Meta Business Verification completes → Business
   Manager can issue a System User → mint an `ads_read` token.~~ **Done.** The
   System User and token were issued 2026-09-04; Business Verification showed
   *Verified* as of 2026-10-26. This token is the same for every client.
2. **One-time, per client:** the client approves Partner access to their ad
   account, and Skypoint assigns that account to the System User and connects it
   to the app. No credential is exchanged; access is granted account-side.
   **Done for Kyrene School District.**
3. **Every call:** send `access_token=<shared token>` and address the client's
   account in the path. There is no session, no login round trip, and nothing to
   refresh.

### The 401 rule (this is the important one)

Meta returns **HTTP 400 with `error.code: 190`** for most OAuth failures, not a
401 — so status code alone cannot classify an auth error. `meta_client.py`
classifies on `error.code`.

Whichever form it arrives in, an auth failure is **never retried**:

| When it happens | What it means | What we do |
|---|---|---|
| First call | The shared token is dead or was never valid | Fail immediately, alert a human |
| Mid-pagination | The shared token was revoked during the pull | Fail immediately, alert a human |

Both are fatal, and for the same reason: the token is static, so there is
nothing a retry could refresh, and Meta locks Business Managers after repeated
bad auth. One retry loop takes down every client's sync simultaneously.

Retryable, with exponential backoff: rate limits (codes 4, 17, 32, 613, 80000–4)
and genuine 5xx/transient errors (codes 1, 2). Nothing else.

---

## 3. PoC validation results (2026-10-27)

The pipeline runs end to end against live data. Results below are from the
Kyrene School District ad account, using a read-only *View performance* grant.

| Check | Result |
|-------|--------|
| Authentication | **Pass.** `SYSTEM_USER` token, valid, never expires, scopes `ads_read,public_profile`, app `1728017481639772` |
| Ad account node | **Pass.** Kyrene School District, ACTIVE, USD, America/Phoenix, US |
| Object counts | **Pass.** 163 campaigns, 195 ad sets, 213 ads, 1,085 creatives |
| Campaign read | **Pass.** Sample returned (enrollment and recruiting campaigns, all ACTIVE) |
| Insights read | **Pass.** Daily campaign-level rows (impressions, clicks, spend, conversions) |
| Extraction → CSV | **Pass.** 20–26 Oct 2026: 27 rows, 80,440 impressions, 3,601 clicks, $1,090.00 spend, 1,055 conversions |
| Normalized schema | 14 columns, one row per campaign per day (§4.3) |
| Cursor pagination | Implemented (`paging.next` followed with no params of our own) |
| Rate budget | Implemented — one shared ceiling, thread-safe, 5 req/s default |
| Auth failure policy | Implemented — fatal on first call *and* mid-pull, zero retries |
| Rate-limit / transient retry | Implemented — backoff honors Meta's `estimated_time_to_regain_access` |
| Edge inventory | **Pass.** 45 edges probed: 20 data, 11 empty, 4 need params, 10 not available, **0 permission-denied** |
| Attribution restatement | **Not yet observed.** Needs two `meta_incremental.py` runs a day apart (§5) |

**Conclusion on permissions:** *View performance* access is sufficient for the
connector's core reads — account, campaigns, ad sets, ads, creatives and
insights. A higher permission level is not needed from the client.

**Note:** `meta_test.py`, the original connectivity script, has been removed.
The connection is now verified by running `meta_edges.py` (read-only) or
`meta_extract.py`. The object counts above came from its final run.

### Setup incident: "API access blocked" (code 200)

During setup every call, including `/me`, failed with
`code=200 subcode=None: API access blocked.` The cause was a **temporary
restriction on the Meta for Developers account**, not the ad-account grant, the
token or the code. Meta's own page later read "Access restored to Meta for
Developers", after which the same token and `.env` worked unchanged.

Diagnostic rule: if `/me` and the app node fail too, the block is app- or
developer-account-level. A read-only grant limit would only affect ad-account
calls. A failure there does not mean the client must grant a higher permission.

### Three run modes

| Mode | Triggered by | What it does |
|---|---|---|
| *(refuses)* | no token, or no `META_AD_ACCOUNT_ID` | Nothing runs; each script names what is missing and exits non-zero |
| `LIVE` | token + account | Full pulls |

There is **deliberately no mock or synthetic mode.** Invented rows are indistinguishable from real ones once they land in a CSV or a warehouse table, so a run started by mistake would contaminate real client data. Without credentials the scripts refuse to run and say what is missing. Every output this package produces is vendor data, or nothing.

---

## 4. Available data ("tables")

Meta has no listable table catalog. The Graph API exposes a fixed set of edges on
the ad account node, and the only way to know which ones a given token can read is
to ask each one — which is what `meta_edges.py` does. **45 edges are catalogued.**
Counts for the core edges are measured (§3); the full accessibility sweep is still
to be run.

### 4.1 The core reporting surface

| Edge | What it holds | Permission |
|------|---------------|-----------|
| `insights` | **The metrics edge.** spend, impressions, clicks, ctr, cpc, cpm, reach, actions | `ads_read` |
| `campaigns` | Campaign objects: name, objective, status, budget | `ads_read` |
| `adsets` | Ad sets: targeting, schedule, bid strategy | `ads_read` |
| `ads` | Individual ads and their creative links | `ads_read` |
| `adcreatives` | Creative bodies, images, links, CTAs | `ads_read` |
| `adimages` / `advideos` | Assets referenced by creatives | `ads_read` |
| `adlabels` | Labels grouping campaigns/ad sets/ads | `ads_read` |

### 4.2 Audiences, tracking, administration

`customaudiences`, `saved_audiences`, `product_audiences`, `customconversions`,
`adspixels` (all `ads_read`) — audience and conversion definitions.

`activities` (change log), `assigned_users`, `agencies`, `applications`,
`promote_pages`, `subscribed_apps`, `publisher_block_lists`, `adrules_library`,
`connected_instagram_accounts`, `partnerintegrations` — mostly require
`ads_management` or `business_management`, which an `ads_read` token will not
have. **Expect these to come back denied, and that is fine** — none are needed
for reporting.

Six edges (`delivery_estimate`, `reachestimate`, `targetingsearch`,
`targetingbrowse`, `generatepreviews`, `customaudiencestos`) will not answer a
bare read; they require query parameters. `meta_edges.py` classifies their
"missing argument" rejection as *reachable*, not *denied*.

Seven more are catalogued only so the report is complete: `users`,
`instagram_accounts`, `offsitepixels`, `broadtargetingcategories`,
`matched_search_applications`, `businessprojects`, `deprecatedtargetingadsets`
— deprecated, and probed purely to confirm they are gone.

### 4.3 The normalized schema this PoC produces

One row per campaign per day. `time_increment=1` is what turns a single date
range into daily rows rather than one rolled-up total for the window.

| Column | Meta source | Note |
|--------|-------------|------|
| `date` | `date_start` | with `time_increment=1`, one row per day |
| `campaign_id`, `campaign_name` | direct | |
| `impressions`, `clicks`, `spend` | direct | **all returned as strings**; cast on the way in |
| `ctr`, `cpc`, `cpm` | direct | omitted entirely when zero — `None` must survive the cast |
| `reach` | direct | unique people; only meaningful at campaign level and above |
| `conversions` | **summed from nested `actions`** | see below |
| `source` | constant `"meta"` | |
| `ad_account_id`, `extracted_at` | PoC metadata | |

**`conversions` is a derived field, and this is a real decision, not a mapping.**
Meta does not return a conversion count. It returns an `actions` array of every
action type that fired — `landing_page_view`, `link_click`, `post_engagement`,
`offsite_conversion.fb_pixel_purchase`, and so on. Which of those *counts* as a
conversion is a business definition. This PoC sums action types matching
`offsite_conversion`, `purchase`, `lead`, `complete_registration`
(`meta_client.CONVERSION_ACTION_TYPES`). **That list needs client sign-off before
production** — it is the difference between two defensible numbers, not between
right and wrong.

### 4.4 Not yet pulled

Instagram placements run inside the same ad account, so their delivery is already
included in the campaign rows above; splitting it out needs the
`publisher_platform` breakdown below.

Ad-set and ad-level detail (`level=adset` / `level=ad`) and audience breakdowns
(`breakdowns=age,gender,country,region,publisher_platform,platform_position,
impression_device`). Both are supported by the same `/insights` call — extra
fields and extra rows on the identical mechanism, not a different one. They
multiply row count substantially, so they are a Silver-layer decision rather
than an automatic Bronze inclusion.

---

## 5. Incremental extraction — the key finding

**Meta has no change feed or version cursor.** `/insights` is a date-windowed
query, and past rows are not immutable. This changes the load semantics, not just
the code.

| | Meta Ads |
|---|----------|
| Mechanism | Date-windowed query; no cursor exists |
| Watermark | Last complete date loaded |
| Are past records immutable? | **No** — insights rows are restated for up to 28 days |
| Load semantics | **`MERGE` / upsert on `(date, campaign_id)`**, never append-only `INSERT` |
| Catching up after a missed run | Automatic from the overlap window |

### Why the rows change

Meta keeps attributing conversions to a click or view for the length of the
attribution window. A conversion that happens on the 10th, from an ad clicked on
the 3rd, is credited to **the 3rd** — and only becomes visible days later. Pull
the 3rd on the 4th and you see roughly half the conversions that day will
eventually be credited. Pull it again a month later and it has settled.

Spend and impressions are effectively stable after a day or two. **Conversions
are not**, and neither is anything derived from them (CPA, ROAS, conversion rate).

### The strategy

```
watermark = last complete date successfully loaded
window    = (watermark − 28 days) .. yesterday
load      = UPSERT on (date, campaign_id)
```

Three things fall out of this, all of them good:

- **The 28-day overlap is not waste.** It is what corrects restated days.
- **A missed run heals itself.** The next run's window already reaches back past
  the gap; there is no catch-up branch to write or test.
- **Today is deliberately excluded.** A partial day's row is guaranteed to be
  restated and adds nothing but noise.

The restatement behaviour is **documented Meta behaviour, not something this PoC
has yet observed.** `meta_incremental.py` is built to detect it: it fingerprints
every row it pulls, stores the watermark, and on the next run reports which
`(date, campaign_id)` pairs came back with different numbers. The live account is
now reachable, but the two runs a day apart have not happened yet, so there is no
measured figure to quote. Run it twice, a day apart, to produce one.

What to expect: spend identical, conversions moved. An append-only load would then
hold both versions of every restated row and double-count the conversions.

### Extraction strategy by data class

| Data class | Edges | Method | Cadence |
|---|---|---|---|
| Reference (small, bounded) | `campaigns`, `adsets`, `ads`, `adcreatives`, `customaudiences` | Full refresh | Daily |
| Metrics (restated) | `insights` | Rolling 28-day window, upsert on `(date, campaign_id)` | Daily |
| Assets (rarely change) | `adimages`, `advideos`, `adlabels` | Full refresh | Weekly, if needed |

### Large accounts: the async path

The synchronous `/insights` call is what this PoC uses and is right for
campaign-level daily data. A large account pulling ad-level data with breakdowns
will time out. Meta's answer is an async report job: `POST /insights` returns a
`report_run_id`, poll `GET /<report_run_id>` until `async_status` is
`Job Completed`, then `GET /<report_run_id>/insights`. Worth knowing exists;
not worth building until a real account proves it necessary.

---

## 6. Reporting suitability

**Strong fits**

1. **Spend and delivery** — daily spend, impressions, clicks, reach per campaign.
   Stable within a day, safe for near-real-time dashboards.
2. **Efficiency trends** — CPC, CPM, CTR over time and across campaigns.
3. **A schema that extends** — the `source` column exists so other sources can
   land in the same table later without reshaping it. Nothing in the pipeline
   assumes Meta is the only writer.
4. **Conversion and CPA reporting** — good, *with the caveat below*.

**Limitations / notes**

- **Conversion figures are provisional for 28 days.** Any dashboard showing
  recent CPA or ROAS should say so, or restrict itself to fully-matured dates.
  This is a reporting-design point, not a pipeline defect.
- **`reach` does not sum.** It is unique people, so daily reach cannot be added
  across days to get weekly reach — that needs a separate call at the wider time
  range. A rollup that sums it is wrong.
- **Currency is account-level.** `spend` is in the ad account's own currency;
  multi-client reporting needs `currency` from the account node and a conversion
  step. Read it from the account node (`GET /act_<id>?fields=currency,timezone_name`).
- **Timezone is account-level too.** `date_start` is in the account's timezone,
  not UTC. Two clients in different timezones do not share a date boundary.
- **`ads_read` is read-only** and will not reach the administrative edges in §4.2.
  Sufficient for analytics; nothing in the reporting path needs more.

---

## 7. Production path — the three-repo build

This PoC is Step 3 raw material only. A real Skypoint V2 connector is registered
and validated through three repos, in order, before it runs for anyone.

| Step | Repo | What gets built for `meta-ads` |
|---|---|---|
| 1 | `skypoint-studio` | `server/seeds/connector-v2-meta-ads.json` — copy `connector-v2-ga4.json`. Field definition carries **only** `adAccountId`, `isCredential: false`. The System User token never appears in a form field because the client never sees it. Logo 120×120 SVG. `pipeline.tasks` ids must match the DAG's Airflow `task_id`s exactly, in execution order. |
| 2 | `skypoint-connector-hub` | `src/connectors/meta-ads/validator.ts` — extends `BaseConnectorValidator`, `connectorName: "meta-ads"`. **One** Graph call, no retry: `GET /me/adaccounts?limit=1`. Register in `registry.ts`; bump the count in `registry.test.ts` (read the current value, don't trust a doc). |
| 3 | `skypoint-skyconnect` | `source-meta-ads/` — scaffold from `source-ga4`, follow `source-paychex` for the process split. |

Studio and SkyConnect are not independent steps: Studio's seed deploy writes the
`DataflowV2` document to the Operations Cosmos DB, and SkyConnect's `sync.py`
reads that same document back as its runtime config.

### What this PoC's files become

| PoC file | Becomes |
|---|---|
| `meta_client.py` — auth, error classification, rate budget | `source_meta_ads/auth.py` + the CDK stream base. The 401 policy ports over unchanged; it is the part that matters. |
| `meta_client.normalize_insight` / `COLUMNS` | The `insights` stream's record shape |
| `meta_client.fetch_insights` / `paginate` | The CDK stream's `read_records` |
| `meta_incremental.py` watermark logic | The stream's incremental cursor + the upsert contract |
| `meta_extract.py` CSV writer | **Deleted.** Replaced by `shared_utils.streaming_upload` writing to the client's own ADLS Gen2 |
| `meta_edges.py` | Not ported — a discovery tool, its job ends at the inventory |

### Shape on disk

```
source-meta-ads/
 ├── sync.py                    outer process — config, buffering, ADLS. Never calls Meta.
 └── source_meta_ads/
     └── run.py                 inner process — Airbyte CDK, auth + streams. Never touches Azure.
```

Two separate Python processes. They communicate **only** by `run.py` printing
structured JSON lines to its own stdout, which `sync.py` reads back — never by
calling each other's functions. That separation is why a stray `print()` inside
`run.py` corrupts the sync, and why only `sync.py` ever holds Azure credentials.

### Streams and entity map

One stream per entity — `campaigns`, `ad_sets`, `ads`, `insights` — plus a
`V2_ENTITY_TO_STREAM` dict translating Studio's entity names to these stream
names. An unmapped entity logs a warning and is skipped; it never silently falls
back to the raw name.

### Where the shared credential lives

The open platform decision (Section 2.1 of the manager-review doc): every
existing connector stores the customer's own credential in the customer's own
tenant Key Vault, and Meta does not fit that shape because the secret is shared.

**Decided: Option A** (go-ahead given Oct 2026) — copy the shared token into each client's own tenant
Key Vault at seed time. It costs zero changes to `v2_config_loader.py`'s
per-instance `keyVaultSecretName` resolution and keeps `meta-ads` indistinguishable
from every other connector at the code level. The cost is that rotating the token
fans out to every client. Settle this **before** the Step 1 seed is written, not
during.

Whichever option wins: **only the credential is shared. The data never is.**
Synced output lands only in that client's own ADLS Gen2.

---

## 8. Reference — PoC scripts (in this folder)

| Script | Purpose |
|---|---|
| `meta_client.py` | Shared config, auth policy, rate budget, normalization |
| `meta_edges.py` | Catalogues/probes all 45 ad-account edges; reports data / empty / needs-params / denied. `--json` for a machine-readable summary |
| `meta_incremental.py` | Detects the restatement problem; implements the watermark + upsert answer. `--reset` clears the stored watermark |
| `meta_extract.py` | Pulls a window, rolls it up, writes normalized CSV to `output/`. Options: `--days`, `--since`, `--until`, `--out` |

**Why one shared module instead of four standalone scripts** — the auth policy is
safety-critical and the credential is shared across every client, so it lives in
exactly one place where it cannot drift between copies.

Dependencies: `requests` only (`python-dotenv` is an optional convenience).
No pandas — the real connector uses the Airbyte CDK and `shared_utils`, so
DataFrame scaffolding here would only be thrown away.

---

## 9. Open items

- [x] ~~**Meta Business Verification**~~ — **done**, *Verified* 2026-10-26.
- [x] ~~**Mint the System User token** (`ads_read`)~~ — **done**,
      `skypoint-ads-integration` / `61593900157271`. Regenerated 2026-10-27.
- [x] ~~**Run live connectivity test**~~ — **done 2026-10-27**, all checks pass (§3).
- [x] ~~**Run `meta_edges.py` live inventory**~~ — **done 2026-10-27**: 20 data, 11 empty, 4 need params, 10 not available, 0 denied.
- [x] ~~**First client ad-account grant**~~ — **done**, Kyrene School District,
      *View performance* (read-only).
- [x] ~~**First live CSV extract**~~ — **done**, 27 rows for 20–26 Oct 2026.
- [ ] **Run `meta_incremental.py` twice, a day apart** to measure the attribution
      restatement for real.
- [ ] **Client sandbox details** — `instance_id`, `tenant_id`, Key Vault name, and
      ADLS path for the first client. **Nothing in Steps 1–3 can start for real
      without this.**
- [x] ~~**Key Vault decision**~~ — **decided: Option A** (copy the shared token into
      each client's tenant Key Vault at seed time). Not to be re-opened.
- [ ] **Get client sign-off on the conversion definition** — which `actions`
      types count (§4.3).
- [ ] **Confirm the Graph API version** against Meta's current release, and note
      its deprecation date.
- [ ] **Confirm the backfill horizon** with the client (Meta retains insights for
      37 months; that is the ceiling, not the answer).
- [ ] Reserve the ACR image name `source-meta-ads` in `crskypointprod001`.
- [ ] Decide whether ad-set/ad-level detail and breakdowns are in Bronze scope
      (§4.4) before the stream list is fixed in the seed.
