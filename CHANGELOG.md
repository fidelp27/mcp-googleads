# Changelog

All notable changes to `google-ads-mcp-server` are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.2.5] - 2026-11-23

### Security / Hygiene

- Replaced a specific 10-digit `customer_id` example that had been
  hardcoded in every write-tool docstring (10 occurrences in
  `google_ads_server.py`) and in four test fixtures with the generic
  placeholder `1234567890` / `123-456-7890`. Previous releases 0.2.0 –
  0.2.4 shipped the specific ID as the "Example" value exposed to MCP
  clients in `tools/list` schemas.
- `.gitignore` now excludes `analisis-*.md` and `keyword-research-*.md`
  so local private research notes cannot be accidentally committed
  with `git add .`.

No behavioral or API changes.

## [0.2.4] - 2026-11-23

### Changed

- README: added a `Support the project` section (GitHub Sponsors +
  Mercado Pago for Argentine donors) and a small `❤ Sponsor` badge
  next to the PyPI / Python / License badges.
- `.github/FUNDING.yml`: enables the ❤ Sponsor button on the repo
  with GitHub Sponsors (`fidelp27`) and Mercado Pago as a custom
  donation link.

Docs-only release — no changes to tools, API surface, or runtime
behavior.

## [0.2.3] - 2026-11-22

### Changed

- Documented installation from PyPI, Google Ads API project access, and browser authorization using each user's Desktop OAuth client JSON.
- Updated the setup examples for Claude Desktop, Claude Code, Codex, and manager accounts.

### Fixed

- Register Antigravity in its current global MCP configuration directory, `~/.gemini/config`, by default.

## [0.2.2] - 2026-11-22

Docs and surface-area cleanup. No behavior changes to tools or
reporting.

### Removed

- The `agency://docs/{domain}` MCP resource. It read Markdown files
  from the project's `docs/` directory, which is not shipped in the
  wheel, so the resource only ever worked from a git checkout. The
  `agency://tools/{tool_name}` resource (with its workflow / safety
  guidance per tool) stays.
- 15 reference docs that were not linked from the README
  (`agency-playbook`, `audiences-assets`, `campaigns`, `client-facing`,
  `conversions-recommendations`, `demand-gen`, `experiments`,
  `gaql-google-ads-query-language`, `great-gaql-samples`,
  `keyword-planner`, `keywords`, `offline-conversions`, `reporting`,
  `reporting-mcc`, `roadmap-marketing`).

### Changed

- `agency/resources.py`: `_tool_domain` now returns a small set of
  domain labels (`guardrails`, `campaigns`, `search`,
  `audiences-assets`, `measurement`, `reporting`, `agency`) used purely
  as informational tags on `agency://tools/{tool_name}` responses.
- README: removed the stale `agency://docs/{domain}` row from the MCP
  Resources table.
- `docs/connect.md` rewritten as a full step-by-step credentials guide
  — Google Cloud project, Google Ads API enablement, OAuth consent
  screen, Desktop OAuth client, developer-token access levels (Test /
  Basic / Standard), `connect` for direct / multi-account / MCC,
  `doctor` verification, common-error troubleshooting, and secret
  storage layout.

## [0.2.1] - 2026-11-22

Documentation and hygiene release. 0.2.0 was yanked on PyPI in favor of
this version (same feature set).

### Changed

- **README** reorganized: installation moved to Section 1 (`uvx` /
  `pipx` / `pip` + demo, `connect` for real accounts, manual
  `claude_desktop_config.json`, and `doctor`), followed by a
  consolidated feature summary and the full 142-tool catalog with
  stable section anchors.
- Added PyPI / Python / License badges.

### Fixed

- `tests/test_audit_fixes.py` no longer references a real client
  filename when asserting `.dockerignore` coverage; the glob
  `analisis-*.md` is now validated with a generic example.

## [0.2.0] - 2026-11-22

First public PyPI release. Major expansion of the modular catalog, governance,
and remote multi-tenant pilot.

### Added

- **Modular tool catalog** with 10 domain modules and 5 presets
  (`core`, `leads`, `ecommerce`, `agency`, `full`) — only 25 `core` tools load
  by default to keep LLM context manageable. All 68 `operations` write tools
  stay excluded from `tools/list` while `READ_ONLY_MODE=true`.
- **`--demo` zero-credential mode** for Claude Desktop, Claude Code, Codex,
  Antigravity, Gemini CLI, and generic MCP clients.
- **`connect` / `doctor` / `register` CLI** to onboard real Google Ads
  accounts over browser OAuth and register the saved profile into each
  supported desktop AI client.
- **Diagnostics module**: `get_impression_share_losses`,
  `get_ad_strength_diagnostics`, `get_auction_insights`,
  `check_consent_signal_coverage`.
- **Measurement module**: `get_enhanced_conversions_status`, offline
  conversions upload (clicks, calls, adjustments).
- **Search module**: Keyword Planner tools, search terms clustering with
  negative suggestions.
- **Demand Gen family**: campaign, ad group, and read tools (v25 API).
- **Experiments module**: Drafts & Experiments tools (v25 Experiment API).
- **MCC module**: `mcc_bulk_apply` for cross-client mutations under a single
  approval; multi-account currency-separated agency dashboard.
- **Customer Match** membership management tools.
- **Governance**: file-based multi-operator resource locking,
  `list_expiring_approvals`, diff preview on every `dry_run`, structured
  `GoogleAdsError` with parsed API envelope, audit log export + rotation.
- **Remote pilot gateway** (`[remote]` extras) with OAuth JWT validation,
  per-tenant worker isolation, Fernet-encrypted refresh tokens, PKCE
  self-serve flow, and idempotent disconnect/revocation.
- **Customizable analysis profiles** (Markdown + TOML), built-in profiles
  packaged in the wheel: `leads-audit`, `ecommerce-audit`, `agency-weekly`.
- **Reporting resolvers**: geo targets, ad group and ad IDs, UTM runbook.
- Support for `final_url_suffix` and `tracking_url_template` on
  `update_campaign`.

### Changed

- Reports now brand as Clariola by default (overridable via
  `config/branding.json`).
- Google Ads API default pinned to `v25`.

### Fixed

- Recommendation query no longer selects the unselectable
  `impact.base_metrics.impressions` field.

## [0.1.0] - 2025-07-15

Initial internal release.
