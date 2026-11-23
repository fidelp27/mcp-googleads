# Google Ads MCP Server (`google-ads-mcp-server`)

> **Español:** Ver la [guía rápida y ejemplos por rol en español](docs/es/README.md) y la [guía de conexión](docs/connect.md).

An open-source, **read-only-by-default** **Model Context Protocol (MCP)** server for auditing, reporting, diagnosing, and safely managing Google Ads accounts from desktop and CLI AI apps (**Claude Desktop, Claude Code, Codex, Antigravity, Gemini CLI, Cherry Studio / Cline / Cursor / VS Code with DeepSeek, Kimi, OpenAI, Anthropic, or Gemini**).

Ships **142 structured tools** across **10 modular domains** with **5 one-word presets**, **editable Markdown + TOML analysis profiles**, **account-timezone & truncation-safe analytics**, **multi-currency / multi-MCC orchestration**, and a **guarded write engine with human-in-the-loop approvals**.

[![PyPI](https://img.shields.io/pypi/v/google-ads-mcp-server.svg)](https://pypi.org/project/google-ads-mcp-server/) [![Python](https://img.shields.io/pypi/pyversions/google-ads-mcp-server.svg)](https://pypi.org/project/google-ads-mcp-server/) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## Table of Contents

1. [Install](#1-install)
   - [A. Zero-credential demo (30 seconds)](#a-zero-credential-demo-30-seconds)
   - [B. Connect a real Google Ads account](#b-connect-a-real-google-ads-account)
   - [C. Manual client configuration](#c-manual-client-configuration)
   - [D. Verify everything (`doctor`)](#d-verify-everything-doctor)
2. [What You Get (Feature Summary)](#2-what-you-get-feature-summary)
3. [Tool Catalog & Presets](#3-tool-catalog--presets---preset----modules)
4. [Complete Tool Reference (All 142 Tools)](#4-complete-tool-reference-all-142-tools)
   - [4.1 `core` Module (25 tools — loaded by default)](#41-core-module-25-tools--loaded-by-default)
   - [4.2 `diagnostics` Module (5 tools)](#42-diagnostics-module-5-tools)
   - [4.3 `measurement` Module (8 tools)](#43-measurement-module-8-tools)
   - [4.4 `search` Module (16 tools)](#44-search-module-16-tools)
   - [4.5 `pmax` Module (8 tools)](#45-pmax-module-8-tools)
   - [4.6 `shopping` Module (6 tools)](#46-shopping-module-6-tools)
   - [4.7 `display` Module (5 tools)](#47-display-module-5-tools)
   - [4.8 `demand-gen` Module (5 tools)](#48-demand-gen-module-5-tools)
   - [4.9 `mcc` Module (11 tools)](#49-mcc-module-11-tools)
   - [4.10 `operations` Module (68 guarded write & governance tools — opt-in only)](#410-operations-module-68-guarded-write--governance-tools--opt-in-only)
5. [MCP Prompts & Resources](#5-mcp-prompts--resources)
6. [Customizable Analysis Profiles (Markdown + TOML)](#6-customizable-analysis-profiles-markdown--toml)
7. [CLI Commands & Scheduled Alerts](#7-cli-commands--scheduled-alerts)
8. [Data Accuracy, Truncation & Safety Architecture](#8-data-accuracy-truncation--safety-architecture)
9. [Client Compatibility & Self-Hosted HTTP](#9-client-compatibility--self-hosted-http)
10. [Development & Documentation](#10-development--documentation)

---

## 1. Install

Install from [PyPI](https://pypi.org/project/google-ads-mcp-server/) with [`uv`](https://docs.astral.sh/uv/getting-started/installation/). The package requires Python 3.11 or later. `uv` can install the Python version requested here.

```sh
uv tool install --python 3.12 google-ads-mcp-server
uv tool update-shell
```

Open a new terminal if the command is not on `PATH`. If you already use `pipx`, `pipx install google-ads-mcp-server` is another way to install it. The single-line commands below work in PowerShell and in macOS/Linux shells.

### A. Zero-credential demo (30 seconds)

Try audits and reports with synthetic accounts (`1111111111` EUR ecommerce and `2222222222` USD leads). This mode requires only the package and an MCP client.

```bash
# Register demo mode in your desktop AI client of choice
google-ads-mcp-server register --client claude-desktop --demo
google-ads-mcp-server register --client codex         --demo --preset ecommerce
google-ads-mcp-server register --client gemini-cli    --demo --preset leads
google-ads-mcp-server register --client generic       --demo                    # prints JSON for Cursor/VS Code/Cherry/Cline/Kimi
```

Or launch directly over `stdio` without registering:

```bash
google-ads-mcp-server --transport stdio --demo --preset ecommerce
```

Restart the client after registration. Then try one of these examples:
- *"List my accessible accounts and build a multi-currency agency dashboard."*
- *"Load the `leads-audit` analysis profile and audit account `2222222222`."*
- *"Diagnose delivery issues, budget pacing, and wasted search terms on account `1111111111`."*
- *"Run `get_pmax_diagnostics` and `get_shopping_performance` on account `1111111111`."*

### B. Connect a real Google Ads account

1. Enable the **Google Ads API** in your own Google Cloud project. For live accounts, request [Explorer access or higher](https://developers.google.com/google-ads/api/docs/api-policy/access-levels#explorer_access_level) for that project.
2. In **Google Auth Platform → Data Access**, add `https://www.googleapis.com/auth/adwords`. Under **Clients**, create a **Desktop app** OAuth client and download its JSON. For an external app in **Testing**, add the Google account that will authorize as a test user. The Ads authorization in that mode expires after seven days. See the [detailed setup guide](docs/connect.md).
3. Run `connect`, paste the path to your downloaded JSON when prompted, and authorize with the Google account that can access the Ads account or MCC. The CLI validates access, saves a local profile, and registers your selected client in read-only mode.

```bash
# Direct account. Replace the example customer ID with yours.
google-ads-mcp-server connect --client claude-desktop --customer 1234567890

# Alternatively, connect all enabled non-manager accounts under your MCC.
google-ads-mcp-server connect --client claude-desktop --manager 9999999999 --all-mcc-clients --preset agency
```

Use `--oauth-client "path/to/client_secret.json"` to pass the JSON path directly. Omit `--customer` for interactive account selection when more than one account is available. To connect just one account under an MCC, pass both `--manager` and `--customer`.

For a different client, change `--client` on `connect`, or register the existing profile after connecting once:

```sh
google-ads-mcp-server register --client codex
google-ads-mcp-server register --client claude-code
```

Supported values are `claude-desktop` (Windows/macOS), `claude-code` (requires `claude` on `PATH`), `codex` (local CLI, desktop, or IDE), `antigravity`, `gemini-cli`, and `generic`. For Antigravity with package version 0.2.2, see the [config path workaround](docs/connect.md#antigravity-con-la-versión-022). If you registered the demo in the same client, remove its `google_ads_mcp` entry before registering the real account.

Restart your client and ask it to use the Google Ads MCP to list accounts or report campaign performance for the last seven days. The [local profile](docs/connect.md#dónde-se-guarda-cada-cosa) contains a refresh token. Keep it private.

### C. Manual client configuration

For a client that supports local `stdio` servers, generate its configuration from your existing profile:

```sh
google-ads-mcp-server register --client generic
```

Copy the generated `google_ads_mcp` entry into your client's configuration using its schema. In Claude Desktop, merge it into `mcpServers` in **Settings → Developer → Edit Config**. For the demo configuration, add `--demo` to `register --client generic`. Restart the client after editing.

### D. Verify everything (`doctor`)

```bash
google-ads-mcp-server doctor            # human-readable, hits the API
google-ads-mcp-server doctor --offline  # skips network, checks config only
google-ads-mcp-server doctor --json     # machine-readable for scripts/CI
```

`doctor` separates credential problems, Cloud project access-level limits, permission issues, and zero-data test accounts — so you know exactly what to fix.

---

## 2. What You Get (Feature Summary)

- **Zero-credential `--demo` sandbox** against two realistic synthetic accounts (EUR ecommerce in `Europe/Madrid` + USD B2B leads in `America/New_York`) — full read, audit, and reporting workflow in 30 seconds.
- **Guided onboarding & preflight** (`connect`, `register`, `doctor`, `serve`): browser OAuth, direct & multi-account/MCC scoping (`--all-mcc-clients`), preset persistence (`--preset`, `--modules`), actionable diagnostics.
- **Compact modular tool surface:** default `core` exposes **25 focused tools** validated against a multi-AI JSON Schema linter (no untyped `dict[str, Any]`), so tool calling stays accurate and low-token across Claude, OpenAI, Gemini, DeepSeek, and Kimi.
- **360° operational diagnostics:**
  - *Delivery & policy:* Combines account status, `billing_setup`, `account_budget`, campaign serving reasons, impression share losses, and ad/asset/keyword disapprovals into `confirmed_causes`, `hypotheses`, and `insufficient_data`.
  - *Budget pacing:* MTD spend, 7-day vs. MTD run rate, end-of-month projection, remaining budget, and required daily spend — in the account's time zone.
  - *Channel depth:*
    - **Search** — search terms, n-gram clusters, Quality Score 3-component breakdown, RSA Ad Strength, Keyword Planner, Experiments, Auction Insights API + EN/ES UI CSV fallback.
    - **Performance Max** — asset groups, v25 asset `primary_status`/approvals, signals, listing filters, per-campaign search themes with explicit sampling coverage, placements.
    - **Shopping** — SKU/brand/category/product-group ROAS and wasted spend.
    - **Display & Demand Gen** — placements, mobile-app waste, video view rate and quartile funnel `p25/p50/p75/p100`.
- **Conversion & offline measurement audits:** Maps conversion ownership (account vs. MCC), account/campaign/custom goals (`conversion_goal_campaign_config`), secondary actions promoted into custom goals, Smart Bidding alignment, double-counting risks, Enhanced Conversions prerequisites, honest primary-vs-all measurement scope, offline upload health without PII, and **Data Manager API** upgrade eligibility.
- **Trustworthy reporting & multi-currency / multi-MCC accuracy:** Account-timezone closed days (`complete_days_only=True`), `queried_at_utc`, `conversion_lag_warning`, interaction-date vs. `conversions_by_conversion_date` series, strictly per-currency agency totals (`managed_totals_by_currency`), dynamic per-account `login-customer-id` routing, bounded search with `truncated` / `totals_are_partial` flags, and automatic downgrade of absence-based alerts to `insufficient_data` when backing queries are truncated.
- **Customizable analysis profiles** (Markdown + TOML) packaged in the wheel: `leads-audit`, `ecommerce-audit`, `agency-weekly`, plus user overrides in `GOOGLE_ADS_CONFIG_DIR/profiles/`.
- **MCC agency governance:** parallel GAQL across clients (`run_across_clients`), consolidated multi-account dashboards strictly separated by currency, pending-approval queue, expiring-approval alerts, exportable append-only audit logs with rotation.
- **Guarded write engine (opt-in only):** When `READ_ONLY_MODE=true` (the default), all **68 write tools** are **hidden from `tools/list`**. When explicitly enabled, mutations enforce mandatory `dry_run=true` previews with diffs, exact confirmation phrases, economic caps (`MAX_DAILY_BUDGET_INCREASE_MICROS`, `MAX_BUDGET_INCREASE_PCT`, `MAX_BID_MICROS`), optimistic locking (`state_verifier`), partial retry for multi-account batches (`mcc_bulk_apply` with `retry_failed_from_approval_id`), PII-redacted append-only audit logs, and `revert_operation` on supported families.
- **Self-hosted HTTP & multi-tenant remote pilot (opt-in):** `--transport streamable-http` (loopback-only by default) for single-user self-hosting, and `google-ads-mcp-server remote` for a multi-user gateway with OAuth JWT validation, per-tenant worker isolation, Fernet-encrypted refresh tokens, PKCE self-serve onboarding (`/connect`), idle-evicting worker pool, and idempotent disconnect + Google revocation. See [Remote Pilot & Self-Serve Guide](docs/remote-pilot.md).
- **Scheduled alerts CLI** (`google-ads-mcp-server alerts`) for cron, GitHub Actions, or Slack/email integrations — exits with code `2` on alert hits.

---

## 3. Tool Catalog & Presets (`--preset` / `--modules`)

Instead of overwhelming LLMs with all 142 tools at once, only `core` (**25 tools**) loads by default. Activate domain modules with `--preset` or `--modules` (in `connect`, `register`, `serve`, CLI flags, or `GOOGLE_ADS_PRESET` / `GOOGLE_ADS_MODULES`):

| Preset | Active Modules | Active Tools (Read-Only) | Best For |
| --- | --- | :---: | --- |
| `core` *(default)* | `core` | 25 | Account overview, GAQL, trends, reports, waste, alerts, analysis profiles |
| `leads` | `core, diagnostics, measurement, search` | 54 | Search lead-gen audits, conversions, delivery, pacing, keywords, RSAs |
| `ecommerce` | `core, diagnostics, measurement, pmax, shopping` | 47 | PMax asset groups & placements, Shopping SKU/brand ROAS, pacing, tracking |
| `agency` | `core, diagnostics, measurement, search, pmax, mcc` | 71 | Multi-account MCC operations, multi-currency dashboards, weekly reviews |
| `full` | All read modules (+ `operations` **only** if `READ_ONLY_MODE=false`) | 74 *(142 when writes enabled)* | Full power-user read or read+write surface |

Fine-grained selection example:

```bash
google-ads-mcp-server --transport stdio --modules core,diagnostics,measurement,pmax
```

> **Hard safety invariant:** While `READ_ONLY_MODE=true` (default), all **68 tools** in `operations` are **completely excluded from `tools/list`** even when `--preset full` is passed.

---

## 4. Complete Tool Reference (All 142 Tools)

### 4.1 `core` Module (25 tools — loaded by default)

Account discovery, safe GAQL, core campaign/ad/creative read, time-series trends, reports, waste detection, alerts, and customizable analysis profiles.

| Tool | What It Does |
| --- | --- |
| `server_status` | Reports server readiness, auth mode, API version, guardrail posture (`read_only_mode`, scoped accounts), and active tool catalog/modules. |
| `list_accounts` | Lists accessible Google Ads customer IDs (or synthetic accounts in `--demo` mode). |
| `get_account_currency` | Retrieves account currency code and metadata. |
| `list_resources` | Queries `googleAdsFieldService` to list available Google Ads API resources for GAQL discovery. |
| `execute_gaql_query` | Runs a read-only GAQL query via the core server endpoint (formatted summary output). |
| `run_gaql` | Runs a read-only GAQL query via the agency engine with bounded row protection and structured JSON output. |
| `get_campaign_performance` | Campaign metrics (impressions, clicks, CTR, CPC, cost, conversions, value, CPA) over configurable windows. |
| `get_ad_performance` | Ad-level performance across campaigns and ad groups. |
| `get_ad_creatives` | Reads RSA headlines, descriptions, paths, final URLs, and approval/ad strength fields. |
| `get_image_assets` | Lists image assets with dimensions, URLs, and metadata. |
| `download_image_asset` | Downloads an image asset locally for visual inspection or review. |
| `get_asset_usage` | Shows which campaigns/ad groups reference image and creative assets. |
| `analyze_image_assets` | Analyzes image asset dimensions, aspect ratios, and performance. |
| `get_budget_utilization` | Compares daily spend against configured campaign budgets to spot budget-capped or underspending campaigns. |
| `get_bidding_strategies` | Lists portfolio and campaign bidding strategies, targets, and campaign counts. |
| `get_change_history` | Reads Google Ads `change_event` history (who changed what resource and when) for troubleshooting. |
| `get_performance_trends` | Account or campaign time series (`total`, `daily`, or `weekly`) in the account's time zone with `conversions` (interaction date) alongside `conversions_by_conversion_date`, `conversion_lag_warning`, and `totals_are_partial`. |
| `compare_periods` | Compares two explicit (`YYYY-MM-DD:YYYY-MM-DD`) or preset (`week`, `month`, `quarter`) periods in the account's time zone with absolute and percentage deltas. |
| `generate_client_report` | Generates an executive client report (`json`, `markdown`, or `html`) with typed `ReportBranding`, period-over-period deltas, top campaigns/keywords, wasted search terms, optional recommendations, and truncation metadata. |
| `generate_agency_dashboard` | Consolidates performance across multiple accounts **strictly separated by currency** (`managed_totals_by_currency`), surfacing alerts, pending approvals, and per-client truncation flags. |
| `detect_waste` | Flags non-converting search terms, low-CTR keywords, non-converting ad groups, and budget-limited campaigns in the account's time zone with `totals_are_partial`. |
| `check_alerts` | Evaluates spend, CTR, CPA, and waste thresholds from `config/alerts.json` (`defaults` + per-account overrides). |
| `generate_optimization_plan` | Produces a prioritized, **non-executing** list of suggested `dry_run=true` MCP actions backed by account evidence. |
| `list_analysis_profiles` | Lists built-in and user-defined Markdown + TOML analysis profiles (`leads-audit`, `ecommerce-audit`, `agency-weekly`, plus custom profiles). |
| `get_analysis_profile` | Returns a profile's full Markdown instructions, effective TOML parameters/thresholds, reproducibility header, and security notice. |

---

### 4.2 `diagnostics` Module (5 tools)

Included in `leads`, `ecommerce`, `agency`, and `full` presets.

| Tool | What It Does |
| --- | --- |
| `diagnose_delivery` | 360° root-cause diagnosis for zero/dropped impressions: inspects account status, `billing_setup`, `account_budget`, campaign `primary_status`/`primary_status_reasons`, budget/rank impression share losses, and ad/asset/keyword policy disapprovals — separating findings into `confirmed_causes`, `hypotheses`, and `insufficient_data` (downgrading absence-based billing conclusions when `billing_setup` is truncated). |
| `get_budget_pacing` | Computes complete-day MTD spend in the account's time zone, 7-day vs. MTD daily run rate, projected end-of-month spend, remaining budget, required daily spend, and status (`on_track`, `underspending`, `overspending`) against an explicit target, TOML profile target, or inferred active daily budgets. |
| `get_landing_page_performance` | Landing page URL metrics (`landing_page_view`) including mobile-friendly click rate (`mobile_friendly_clicks_percentage`) and `speed_score`. |
| `get_demographics_performance` | Performance breakdowns by `age`, `gender`, `income`, `parental_status`, or `all`. |
| `get_assets_and_calls_performance` | Extension/asset metrics and policy approvals (`campaign_asset`) plus aggregate call metrics (`call_view`, zero PII). |

---

### 4.3 `measurement` Module (8 tools)

Included in `leads`, `ecommerce`, `agency`, and `full` presets.

| Tool | What It Does |
| --- | --- |
| `audit_conversion_tracking` | Unified conversion audit crossing `customer.conversion_tracking_setting` (MCC vs. account ownership, Customer Data Terms, EC4L), `conversion_action`, `customer_conversion_goal`, `campaign_conversion_goal`, `custom_conversion_goal`, `conversion_goal_campaign_config` (v25), campaign bidding strategies, and per-action volume. Flags MCC centralization, secondary actions inside custom goals, multiple primaries in the same category, and Smart Bidding alignment (degrading absence-based alerts to `insufficient_data` on truncation/optional-query failure). |
| `get_offline_conversion_diagnostics` | Inspects `offline_conversion_upload_client_summary` and `offline_conversion_upload_conversion_action_summary` (sent, accepted, pending, failed, error codes — **zero PII**), distinguishes API ingestion from ad attribution, and flags legacy upload integrations eligible for **Data Manager API** upgrade. |
| `get_conversion_actions` | Lists conversion actions with category, origin, type, attribution model, counting type, default value, and `primary_for_goal`. |
| `get_conversion_performance` | Conversion volume, value, and cost per conversion segmented by conversion action. |
| `get_enhanced_conversions_status` | Checks customer-level Enhanced Conversions for Leads and Customer Data Terms acceptance status. |
| `check_consent_signal_coverage` | Reports primary-vs-all conversion scope ratios (`conversions / all_conversions`) honestly as a measurement-scope metric (explicitly noting that Google Ads API does not expose per-event Consent Mode signals). |
| `get_customer_match_list_stats` | Reads CRM-based `user_list` membership status, size for Search/Display, match rate range, and eligibility. |
| `get_offline_user_data_job_status` | Tracks async `offline_user_data_job` status and failure reasons for Customer Match uploads. |

---

### 4.4 `search` Module (16 tools)

Included in `leads`, `agency`, and `full` presets.

| Tool | What It Does |
| --- | --- |
| `get_search_terms_report` | Search queries (`search_term_view`) with status (`ADDED`/`EXCLUDED`/`NONE`), spend, clicks, conversions, and value. |
| `get_keyword_performance` | Keyword metrics (`keyword_view`) with match type, Quality Score, and all **3 Quality Score subcomponents** (`creative_quality_score`, `post_click_quality_score`, `search_predicted_ctr`). |
| `get_impression_share_losses` | Campaign Search Impression Share, exact-match IS, and IS lost to budget vs. rank. |
| `get_ad_strength_diagnostics` | Audits Responsive Search Ad strength (`POOR`/`AVERAGE`/`GOOD`/`EXCELLENT`), policy approvals, and pinning. |
| `get_auction_insights` | Queries API `auction_insight_*` metrics when available to the Cloud project. |
| `analyze_auction_insights_csv` | Parses Google Ads UI Auction Insights CSV exports (**English and Spanish**) into competitor rankings and high-pressure overlap alerts whenever the API restricts `auction_insight_*`. |
| `get_shared_sets` | Lists shared negative keyword lists and campaign attachments. |
| `analyze_search_terms_clusters` | Groups search terms into n-gram clusters with aggregate spend, clicks, CTR, conversions, and CPA. |
| `suggest_negatives_from_clusters` | Suggests candidate negative keywords from high-spend, zero-conversion search term clusters (read-only advisory). |
| `generate_keyword_ideas` | Uses Google Ads Keyword Planner to discover new keyword ideas with search volumes and competition. |
| `generate_keyword_historical_metrics` | Retrieves historical monthly search volume, competition, and top-of-page bid ranges for seed keywords. |
| `generate_keyword_forecast` | Forecasts clicks, impressions, cost, and conversions for a keyword plan. |
| `find_geo_target_constants` | Searches Google Ads `geo_target_constant` IDs by city, region, or country name. |
| `list_experiments` | Lists Search/Performance experiments and their status. |
| `get_experiment` | Retrieves configuration and arms (control vs. treatment) of an experiment. |
| `get_experiment_results` | Compares control vs. treatment arm metrics with lift/delta calculations. |

---

### 4.5 `pmax` Module (8 tools)

Included in `ecommerce`, `agency`, and `full` presets.

| Tool | What It Does |
| --- | --- |
| `get_pmax_diagnostics` | Full Performance Max audit: `asset_group` strength/metrics, `asset_group_asset` v25 `primary_status`/`primary_status_reasons`/approvals, `asset_group_signal` (audiences & search themes), `asset_group_listing_group_filter`, per-campaign `campaign_search_term_insight` (top 10 PMax campaigns), and `performance_max_placement_view` (mobile-app impression alerts). Explicitly reports `coverage_status` (`complete` / `empty` / `sampled` / `partial_error`), `truncated_queries`, `search_term_insights_coverage`, `placements_coverage`, and `sampling_warnings`. |
| `analyze_auction_insights_csv` | Parses Google Ads UI Auction Insights CSV exports (EN/ES). |
| `get_geo_performance` | Performance breakdown by geographic location. |
| `get_device_performance` | Performance breakdown by device (`MOBILE`, `DESKTOP`, `TABLET`, `CONNECTED_TV`). |
| `get_time_of_day_performance` | Performance breakdown by day of week and hour of day. |
| `get_audience_performance` | Performance breakdown across audience segments. |
| `list_recommendations` | Lists active Google Ads recommendations (read-only review). |
| `get_recommendation_detail` | Inspects impact estimates and parameters of a specific recommendation. |

---

### 4.6 `shopping` Module (6 tools)

Included in `ecommerce` and `full` presets.

| Tool | What It Does |
| --- | --- |
| `get_shopping_performance` | Shopping performance grouped by `product` (SKU), `brand`, `category` (`shopping_performance_view`), or `product_group` (`product_group_view`), returning blended ROAS, `top_winners`, `wasted_products` above `min_waste_spend`, wasted-spend share %, and `totals_are_partial`. |
| `analyze_auction_insights_csv` | Parses Shopping Auction Insights UI CSV exports (EN/ES). |
| `get_geo_performance` | Geographic performance breakdown. |
| `get_device_performance` | Device performance breakdown. |
| `list_recommendations` | Lists Shopping/account recommendations. |
| `get_recommendation_detail` | Inspects a recommendation in detail. |

---

### 4.7 `display` Module (5 tools)

Included in `full` preset (or via `--modules display`).

| Tool | What It Does |
| --- | --- |
| `get_placements_and_video_performance` | Audits `detail_placement_view` (surfacing zero-conversion and `MOBILE_APPLICATION` exclusion candidates) alongside campaign video view rate, average CPV, and quartile funnel (`p25/p50/p75/p100`). |
| `get_audience_performance` | Audience segment performance. |
| `get_geo_performance` | Geographic performance. |
| `get_device_performance` | Device performance. |
| `get_time_of_day_performance` | Day-of-week & hour-of-day performance. |

---

### 4.8 `demand-gen` Module (5 tools)

Included in `full` preset (or via `--modules demand-gen`).

| Tool | What It Does |
| --- | --- |
| `get_demand_gen_performance` | Demand Gen campaign & ad group performance summary. |
| `get_placements_and_video_performance` | Placement brand-safety audit + video view rate & quartile retention (`p25/p50/p75/p100`). |
| `get_audience_performance` | Audience segment performance. |
| `get_geo_performance` | Geographic performance. |
| `get_device_performance` | Device performance. |

---

### 4.9 `mcc` Module (11 tools)

Included in `agency` and `full` presets.

| Tool | What It Does |
| --- | --- |
| `list_client_accounts` | Discovers active non-manager child accounts under a Manager Account (MCC) and automatically registers dynamic per-account `login-customer-id` routing for the session. |
| `get_client_context` | Returns the active client context and configured client metadata. |
| `switch_context` | Switches the active client context for multi-account agency sessions. |
| `run_across_clients` | Runs a read-only GAQL query in parallel across multiple client accounts (respecting per-account MCC routing and `ALLOWED_CUSTOMER_IDS`). |
| `generate_agency_dashboard` | Multi-account consolidated dashboard separated by currency. |
| `get_audit_log` | Reads local append-only governance audit entries with filters (account, tool, status, date). |
| `get_pending_approvals` | Lists pending human-approval previews awaiting confirmation. |
| `list_expiring_approvals` | Highlights pending approvals nearing their TTL expiration. |
| `list_pending_client_approvals` | Lists approvals requiring explicit client sign-off when `REQUIRE_CLIENT_APPROVAL=true`. |
| `export_audit_log` | Exports filtered audit logs to JSON or CSV for compliance/client reporting. |
| `rotate_audit_log` | Archives older daily audit JSONL files into compressed zip archives per retention policy. |

---

### 4.10 `operations` Module (68 guarded write & governance tools — opt-in only)

**Hidden from `tools/list` whenever `READ_ONLY_MODE=true`.** Only exposed when `READ_ONLY_MODE=false` and `operations` (or `--preset full`) is enabled. Every mutation requires `dry_run=true` first (`approval_id` returned) before `dry_run=false`. See [Write Safety Guarantees by Family](docs/compatibility-matrix.md#5-write-safety-guarantees-by-family-operations-module) for exact `validateOnly`, `state_verifier`, and `revert_operation` coverage per family.

- **Campaigns & Budgets (13):** `create_campaign_budget`, `update_campaign_budget` *(optimistic locking + revert)*, `create_search_campaign`, `create_pmax_campaign`, `create_display_campaign`, `create_campaign_from_template`, `update_campaign`, `pause_campaign` *(optimistic locking + revert)*, `enable_campaign` *(optimistic locking + revert)*, `remove_campaign` *(optimistic locking)*, `update_campaign_geo_targets`, `update_campaign_schedule`, `copy_campaign`.
- **Ad Groups & Ads (11):** `create_ad_group`, `update_ad_group`, `pause_ad_group`, `enable_ad_group`, `remove_ad_group`, `create_responsive_search_ad`, `create_call_ad`, `update_ad`, `pause_ad`, `enable_ad`, `remove_ad`.
- **Keywords, Bids & Negatives (10):** `add_keywords_to_ad_group`, `update_keyword_bid` *(optimistic locking + revert)*, `pause_keyword` *(revert)*, `enable_keyword` *(revert)*, `remove_keyword`, `add_negative_keyword`, `add_negative_keywords_bulk`, `create_negative_keyword_list`, `attach_negative_keyword_list_to_campaign`, `add_account_negative_keywords` *(applies across Search, Shopping & PMax)*.
- **Audiences, Bid Modifiers & Customer Match (6):** `add_audience_to_ad_group`, `remove_audience_from_ad_group`, `update_bid_modifier`, `create_customer_match_list`, `add_customer_match_members` *(local SHA-256 hashing, zero raw PII in logs)*, `remove_customer_match_members`.
- **Assets, Extensions & Placements (8):** `create_sitelink_extension`, `create_callout_extension`, `create_structured_snippet_extension`, `create_call_extension`, `create_image_asset`, `link_asset_to_campaign`, `unlink_asset_from_campaign`, `exclude_placements` *(campaign or account level, websites & mobile apps, deduplicated up to 100)*.
- **PMax, Demand Gen, Experiments & Recommendations (8):** `create_pmax_asset_group`, `create_demand_gen_campaign`, `create_demand_gen_ad_group`, `create_experiment`, `schedule_experiment`, `end_experiment`, `apply_recommendation`, `dismiss_recommendation`.
- **Conversion Actions, Goals & Offline Uploads (8):** `create_conversion_action`, `update_conversion_action`, `pause_conversion_action`, `update_campaign_conversion_goal` *(optimistic locking + revert)*, `set_conversion_action_primary` *(optimistic locking + revert)*, `upload_click_conversions`, `upload_call_conversions`, `upload_conversion_adjustments`.
- **MCC Bulk & Write Governance (4):** `mcc_bulk_apply` *(multi-account guarded batch with per-item execution tracking & `retry_failed_from_approval_id` partial retry)*, `approve_operation`, `reject_operation`, `revert_operation`.

---

## 5. MCP Prompts & Resources

Clients that support MCP Prompts and Resources automatically receive:

| Type | Identifier | Purpose |
| --- | --- | --- |
| **Prompt** | `analysis_profile_prompt` | Loads any Markdown + TOML analysis profile (`leads-audit`, `ecommerce-audit`, `agency-weekly`, or custom user profile) with reproducibility header and parameters. |
| **Prompt** | `google_ads_workflow` | Guided step-by-step Google Ads analysis workflow. |
| **Prompt** | `gaql_help` | GAQL query construction assistance and best practices. |
| **Prompt** | `monthly_client_review` | Structured monthly performance & optimization review prompt. |
| **Prompt** | `safe_campaign_change` | Guided `dry_run -> review diff -> approval_id -> execute` change workflow. |
| **Resource** | `agency://governance/status` | Live read-only snapshot of active guardrails, limits, and operator settings. |
| **Resource** | `gaql://reference` | Compact GAQL field & resource reference. |
| **Template** | `agency://tools/{tool_name}` | Tool-level usage documentation and safety notes. |

> **Multi-AI self-containment:** Critical workflows (analysis profiles, `dry_run` rules, currency/timezone metadata, truncation flags) are also exposed directly through tools so clients that ignore MCP Prompts/Resources work with zero loss of functionality.

---

## 6. Customizable Analysis Profiles (Markdown + TOML)

Built-in profiles are packaged inside the wheel (`agency/builtin_profiles/` + `config/profiles/`) so they work even when installed via `pip`/`uvx` outside a git checkout:
- **`leads-audit`** (`.toml` + `.md`): Lead-gen Search audit covering conversion goals, primary vs. secondary separation, delivery, pacing, wasted search terms, and Quality Score.
- **`ecommerce-audit`** (`.toml` + `.md`): Ecommerce audit covering PMax asset groups/placements, Shopping SKU/brand ROAS & waste, conversion tracking, and budget pacing.
- **`agency-weekly`** (`.toml` + `.md`): Multi-account weekly executive review separated by currency with alerts and prioritized next actions.

**User customization:** Place `<profile-id>.toml` and/or `<profile-id>.md` in `GOOGLE_ADS_CONFIG_DIR/profiles/` to create custom profiles or override any built-in profile. TOML thresholds automatically merge over `alerts.json` defaults.

**Security boundary:** Profiles control analysis methodology, target KPIs, and report structure only. They **cannot** override `READ_ONLY_MODE`, `ALLOWED_CUSTOMER_IDS`, economic caps, or human approval requirements.

---

## 7. CLI Commands & Scheduled Alerts

The lightweight CLI dispatcher (`google_ads_cli.py`) powers all subcommands without pre-importing the server or `.env` before managed profile initialization:

```bash
# 1. Start MCP server directly (stdio or streamable-http)
google-ads-mcp-server --transport stdio --demo --preset ecommerce
google-ads-mcp-server --transport streamable-http --host 127.0.0.1 --port 8000

# 2. Browser OAuth onboarding + client registration
google-ads-mcp-server connect --client claude-desktop --customer 1234567890 --preset leads

# 3. Register an existing profile or --demo in another desktop client
google-ads-mcp-server register --client codex --preset agency
google-ads-mcp-server register --client generic --demo

# 4. Preflight diagnostics (live or offline, human or JSON)
google-ads-mcp-server doctor
google-ads-mcp-server doctor --offline --json

# 5. Serve a managed profile directly (read-only enforced, uses saved or CLI preset)
google-ads-mcp-server --profile /path/to/profile.json serve --preset ecommerce

# 6. Scheduled alerts CLI (for cron, GitHub Actions, Slack/email scripts)
google-ads-mcp-server alerts --profile leads-audit --format markdown --fail-on-alerts
google-ads-mcp-server alerts --demo --profile leads-audit --format json
```

`alerts --fail-on-alerts` exits with code `2` whenever one or more threshold alerts fire.

---

## 8. Data Accuracy, Truncation & Safety Architecture

- **Account time zone & closed days:** Reporting and diagnostic tools resolve date windows in each account's `customer.time_zone` (with `tzdata` on Windows) and exclude the current incomplete day by default (`complete_days_only=True`). Every response includes `currency_code`, `time_zone`, `queried_at_utc`, and `conversion_lag_warning` when the window ends within the last 3 days.
- **No silent truncation or false-positive absence alerts:** Bounded searches cap pagination at `GOOGLE_ADS_MAX_SEARCH_ROWS` (default `2000`) and propagate `truncated`, `totals_are_partial`, `truncated_queries`, `truncation_warning`, and `coverage_complete: false`. Absence-based alerts (`SMART_BIDDING_WITHOUT_EFFECTIVE_ACTIONS`, `PRIMARY_ACTION_NO_VOLUME`, `NO_RECENT_OFFLINE_UPLOADS`, `NO_APPROVED_BILLING_SETUP`) automatically downgrade to `insufficient_data` whenever their backing query is truncated (or an optional goal/summary query fails).
- **Strict multi-currency & multi-MCC isolation:** Agency dashboards never sum metrics across different currencies (`managed_totals_by_currency`), and dynamic `login-customer-id` routing lets one session switch cleanly between direct accounts and multiple MCCs (`RuntimeContext` + `GOOGLE_ADS_LOGIN_CUSTOMER_MAP` + `list_client_accounts`).
- **Loopback-only HTTP by default:** `streamable-http` defaults to `127.0.0.1` (including inside `Dockerfile`) and refuses to bind to non-loopback hosts (`0.0.0.0`, etc.) unless `MCP_HTTP_ALLOW_REMOTE=true` is explicitly set behind an authenticated HTTPS proxy.
- **PII & hygiene protection:** Audit logs redact sensitive payloads; offline conversion diagnostics and Customer Match never log raw PII; automated hygiene tests block tracked credentials or client data.

---

## 9. Client Compatibility & Self-Hosted HTTP

- **Local `stdio`:** Use `connect` or `register` for Claude Desktop, Claude Code, local Codex, Antigravity, and Gemini CLI. For other `stdio` hosts, use the generated configuration from `--client generic`. See the [compatibility matrix](docs/compatibility-matrix.md).
- **ChatGPT in the browser:** Set up a separate remote MCP connection using an HTTPS endpoint or Secure MCP Tunnel in a supported account or workspace. See [OpenAI's custom MCP setup](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt).
- **Self-hosted remote pilot (`Streamable HTTP` + OAuth):**
  - Single-user self-hosted mode via `google-ads-mcp-server --transport streamable-http` (loopback-only unless `MCP_HTTP_ALLOW_REMOTE=true` behind an authenticated HTTPS proxy).
  - Multi-user OAuth gateway via `google-ads-mcp-server remote` supporting both static managed pilots (`MCP_REMOTE_WORKERS`) and **self-serve hosted onboarding** (`/connect` Google OAuth + PKCE, Fernet-encrypted SQLite profile store, lazy-spawning idle-evicting worker pool, per-user presets, and 1-click token revocation). See [Remote Pilot & Self-Serve Guide](docs/remote-pilot.md).

---

## 10. Development & Documentation

- [Compatibility Matrix (AI Apps, Channels, API Limits, Write Guarantees)](docs/compatibility-matrix.md)
- [Guía rápida y playbooks por rol (Español)](docs/es/README.md) | [Guía de conexión (Español)](docs/connect.md)
- [Operational Design](DESIGN.md) | [Guardrails](docs/guardrails.md) | [Release Checklist](docs/release-checklist.md)
- [Roadmap v3 (Desktop Beta & Acceptance)](docs/roadmap-marketing-v3.md) | [Roadmap v2 & Technical Log](docs/roadmap-marketing-v2.md)

Run the full automated test suite:

```bash
pip install -e ".[dev]"
python -m pytest -q
```

## Support the project

`google-ads-mcp-server` is free and open source (MIT). If it's useful to you
and you'd like to say thanks, any contribution helps keep it maintained:

- [**❤ Sponsor on GitHub**](https://github.com/sponsors/fidelp27) — any amount,
  one-time or monthly (USD, via Stripe).
- [**Mercado Pago (Argentina)**](https://link.mercadopago.com.ar/fidelp27) —
  donar en pesos.

No tiers, no perks — just support if you feel like it.

## License

MIT. See [LICENSE](LICENSE).
