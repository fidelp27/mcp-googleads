# Compatibility Matrix — `google-ads-mcp-server`

This document declares verified desktop MCP clients, domain modules, channel coverage, and Google Ads API boundaries for v1.0.

---

## 1. AI Apps & MCP Clients

| Ecosystem / Model | Client | Transport (v1.0) | Registration (`--client`) | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| **Anthropic (Claude)** | Claude Desktop (Windows / macOS) | `stdio` | `claude-desktop` | Primary (config verified; desktop UI E2E pending) | Supports MCP prompts (`analysis_profile_prompt`), resources, and `--preset` / `--modules` via `connect` / `register`. `.mcpb` desktop bundle is **planned** (not yet built). |
| **Anthropic (Claude)** | Claude Code | `stdio` | `claude-code` | Primary (config verified) | Registers via `claude mcp add-json --scope user` (supports `--preset` / `--modules`). |
| **OpenAI** | Codex (CLI / Desktop / IDE) | `stdio` | `codex` | Primary (config verified) | Appends `[mcp_servers.google_ads_mcp]` to `~/.codex/config.toml` (supports `--preset` / `--modules`). |
| **Google (Gemini)** | Antigravity | `stdio` | `antigravity` | Primary (config verified) | Writes `~/.gemini/antigravity/mcp_config.json`; all read schemas pass strict flat-object linter. |
| **Google (Gemini)** | Gemini CLI | `stdio` | `gemini-cli` | Primary (config verified) | Writes `~/.gemini/settings.json`. |
| **DeepSeek** | Desktop MCP hosts (Cherry Studio, Cline, Roo Code) configured with DeepSeek API | `stdio` | `generic` | Supported | DeepSeek API performs tool calling; the desktop client holds the `stdio` MCP connection. Keep `--preset core` or targeted modules (≤35 tools) for best accuracy. |
| **Kimi (Moonshot)** | Kimi CLI / Cherry Studio configured with Kimi API | `stdio` | `generic` | Supported | Use `list_analysis_profiles` / `get_analysis_profile` tools rather than relying on MCP prompts/resources. |
| **Universal IDEs / Hosts** | Cursor, VS Code Copilot, Windsurf, Zed | `stdio` | `generic` | Supported | Paste output of `google-ads-mcp-server register --client generic [--preset ...]`. |
| **Web / Mobile Apps** | ChatGPT web/mobile, Claude.ai web/mobile | Remote `Streamable HTTP` + OAuth 2.1 | — | **Planned v1.1** | Not in v1.0 exit criteria. Local `streamable-http` defaults to `127.0.0.1` and refuses non-loopback hosts unless `MCP_HTTP_ALLOW_REMOTE=true` is explicitly set behind an authenticated HTTPS proxy. |

---

## 2. Domain Modules & Presets

All tools are validated by the multi-AI JSON Schema linter (`tests/test_phase1_catalog_profiles.py`, `tests/test_phase2_measurement.py`, `tests/test_phase3a_diagnostics.py`, `tests/test_phase3b_channels.py`, `tests/test_phase4_operations.py`).

| Module | Default | Read / Write | Summary |
| --- | :---: | --- | --- |
| `core` | Yes (25 tools) | Read-only (+ local image download) | `server_status`, `list_accounts`, GAQL, campaign/ad/creative read, `get_performance_trends`, client report, multi-currency agency dashboard, period comparison, waste, alerts, optimization plan, analysis profiles. |
| `diagnostics` | In `leads`, `ecommerce`, `agency`, `full` | Read-only | `diagnose_delivery` (billing + policy + status), `get_budget_pacing`, landing pages, demographics, campaign assets & call view. |
| `measurement` | In `leads`, `ecommerce`, `agency`, `full` | Read-only | `audit_conversion_tracking` (MCC/account/campaign/custom goals), `get_offline_conversion_diagnostics` (+ Data Manager eligibility), conversion actions/performance, Enhanced Conversions, primary-vs-all ratio, Customer Match stats. |
| `search` | In `leads`, `agency`, `full` | Read-only | Search terms, keywords (with 3 Quality Score subcomponents), impression share losses, RSA Ad Strength, Auction Insights (API + CSV fallback), shared sets, clustering, Keyword Planner, experiments read. |
| `pmax` | In `ecommerce`, `agency`, `full` | Read-only | `get_pmax_diagnostics` (asset groups, v25 asset `primary_status`/approvals, signals, listing filters, per-campaign search themes, placements), geo/device/schedule/audience, recommendations, Auction Insights CSV. |
| `shopping` | In `ecommerce`, `full` | Read-only | `get_shopping_performance` (product/brand/category/product_group ROAS & waste), Auction Insights CSV, geo/device, recommendations. |
| `display` | In `full` | Read-only | `get_placements_and_video_performance` (placement waste / mobile apps), audience/geo/device/schedule. |
| `demand-gen` | In `full` | Read-only | `get_demand_gen_performance`, `get_placements_and_video_performance` (video view rate & quartiles), audience/geo/device. |
| `mcc` | In `agency`, `full` | Read-only (+ local audit export/rotation) | `list_client_accounts` (auto-registers dynamic MCC routing), client context, `run_across_clients`, audit logs, pending/expiring approvals. |
| `operations` | Opt-in only (`READ_ONLY_MODE=false`) | Guarded Write | 68 guarded mutations (campaigns, budgets, ads, keywords, negatives, account negatives, placement exclusions, conversion goals/`primary_for_goal`, PMax asset groups, Demand Gen, experiments, offline uploads, Customer Match, `mcc_bulk_apply` with partial retry, approvals & `revert_operation`). Optimistic locking (`state_verifier`) is enforced on `update_campaign_budget`, `pause_campaign`, `enable_campaign`, `remove_campaign`, `update_keyword_bid`, `update_campaign_conversion_goal`, and `set_conversion_action_primary`. **Hidden from `tools/list` when `READ_ONLY_MODE=true`.** |

---

## 3. Channel Coverage Matrix

| Channel | Read & Diagnostics | Guarded Creation / Edit | Exclusions | Notes |
| --- | --- | --- | --- | --- |
| **Search** | Full (campaigns, ads, RSA strength, keywords + QS breakdown, search terms, IS losses, landing pages, demographics, trends, experiments) | Full (campaigns, budgets, ad groups, RSAs, Call Ads, keywords, bids, geo, schedules, experiments) | Campaign, ad-group, shared list, and account negative keywords | Primary daily-ops channel. |
| **Performance Max** | Full (`get_pmax_diagnostics`: asset groups, v25 asset `primary_status`/approvals, signals, listing filters, per-campaign search themes with explicit sampling coverage, placements) | Campaign & asset group creation, budgets, conversion goals | Account negative keywords & account placement exclusions | Product feed policy depth via Merchant API planned post-v1.0. |
| **Shopping** | Full (`get_shopping_performance` by SKU/brand/category/product group + waste) | Budgets, campaign updates, conversion goals | Account/campaign negative keywords | Merchant Center API diagnostics planned post-v1.0. |
| **Display** | Placements (`detail_placement_view`), audiences, geo, device, schedule, assets | Campaign creation, budgets, status, audiences | Campaign & account website/mobile-app placement exclusions | Standalone Display creative creation kept minimal as Google migrates to Demand Gen. |
| **Demand Gen** | Channel performance, placements, video view rate & quartile funnel (`p25/p50/p75/p100`) | Campaign & ad group creation with channel controls | Campaign & account placement exclusions | Multi-format ad creation planned post-v1.0. |
| **Video / YouTube, App, Hotel** | Basic campaign/video metrics via `get_placements_and_video_performance` and GAQL | Not dedicated in v1.0 | — | Deferred to post-v1.0 based on pilot demand. |

---

## 4. Known Google Ads API Boundaries & Workarounds

- **Auction Insights (`auction_insight_*`):** Restricted per developer token / Cloud project by Google. When `METRIC_ACCESS_DENIED` occurs, export the report from Google Ads UI (`Insights > Auction insights`) and pass the file to `analyze_auction_insights_csv` (English and Spanish exports supported).
- **Consent Mode signals (`ad_user_data`, `ad_personalization`):** Not exposed per event in the Google Ads API. Tools report `conversions / all_conversions` strictly as a primary-vs-secondary measurement difference, never as proof of Consent Mode failure.
- **Test accounts:** Google Ads test accounts validate permissions and API contracts, but produce zero delivery metrics. Use `--demo` for realistic multi-currency exploration and authorized pilot accounts for live validation.
- **Offline uploads & Data Manager API:** `get_offline_conversion_diagnostics` tracks ingestion without PII and detects legacy upload sources eligible for Data Manager API; legacy `ConversionUploadService` writers remain supported while Data Manager API write ingestion is planned post-v1.0.

---

## 5. Write Safety Guarantees by Family (`operations` module)

All 68 write tools live in the `operations` module and are **completely hidden from `tools/list`** whenever `READ_ONLY_MODE=true` (the default, and always enforced in managed desktop profiles). When write mode is explicitly enabled (`READ_ONLY_MODE=false`), each family enforces:

| Family / Module | `dry_run` + `approval_id` + Audit Log | Google `validateOnly` | Optimistic Locking (`state_verifier`) | `revert_operation` Support | Notes |
| --- | :---: | :---: | :---: | :---: | --- |
| **Core campaigns, budgets & keywords** (`agency/write_tools.py`) | Yes (`execute_mutation`) | Yes | Yes on `update_campaign_budget`, `pause_campaign`, `enable_campaign`, `remove_campaign`, `update_keyword_bid` | Yes (budgets, campaign/ad-group/ad/keyword status, keyword bids) | Remaining create/add operations use idempotency keys and `validateOnly`. |
| **Conversion goals, placements & account negatives** (`agency/operations_tools.py`) | Yes (`execute_mutation`) | Yes | Yes on `update_campaign_conversion_goal`, `set_conversion_action_primary` | Yes (`update_campaign_conversion_goal`, `set_conversion_action_primary`) | `exclude_placements` and `add_account_negative_keywords` are additive/deduplicated with cap of 100. |
| **Experiments** (`agency/experiment_tools.py`) | Yes (`create_approval` / `validate_approval`) | Partial (`schedule_experiment` supports `validateOnly`) | Not applicable (multi-step experiment/arm creation) | Manual (`end_experiment`) | Two-step API pipeline (`experiments` + `experimentArms`). |
| **Demand Gen** (`agency/demand_gen_tools.py`) | Yes (`create_approval` / `validate_approval`) | Preview-only | Not applicable (resource creation) | Use `pause_campaign` / `pause_ad_group` | Creates campaigns and ad groups with explicit channel controls. |
| **Customer Match** (`agency/customer_match_tools.py`) | Yes (`create_approval` / `validate_approval`) | Preview-only | Not applicable (async `OfflineUserDataJob`) | Counter-operation (`remove_customer_match_members`) | SHA-256 local hashing, zero raw PII persisted in audit logs. |
| **Offline Conversions** (`agency/offline_conversions_tools.py`) | Yes (`create_approval` / `validate_approval`) | Optional (`validate_only` parameter) | Not applicable (append-only event upload) | `upload_conversion_adjustments` (retract/restate) | Legacy `ConversionUploadService` writers; Data Manager API write migration planned post-v1.0. |
| **MCC Bulk Apply** (`agency/mcc_tools.py`) | Yes (`create_approval` / `validate_approval`) | Delegated per target | Per-target execution tracking (`executed_customer_ids` / `failed_targets`) | Per-account via `revert_operation` | Supports partial retry (`retry_failed_from_approval_id`) without re-applying succeeded targets. |
