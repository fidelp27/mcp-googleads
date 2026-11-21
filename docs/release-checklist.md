# Release Checklist — `google-ads-mcp-server` v1.0

Use this checklist before tagging `v1.0.0` and making the repository or PyPI package public.

## 1. Security, Privacy & Git History
- [ ] Run `python -m pytest tests/test_hygiene.py -q` and confirm `0` failures.
- [ ] Confirm `git status` has no untracked client reports (e.g. local `analisis-*.md`), `.env`, `client_secret.json`, or `*_token.json` files.
- [ ] **Git history audit (mandatory before making repo public):** Earlier commits prior to Phase 0 sanitization contained real client config/PDF files. Purge sensitive historical blobs with `git filter-repo` (or publish from a clean squashed release repository) and verify with `git log --all --stat`.
- [ ] Confirm `READ_ONLY_MODE=true` is the default across `.env.example`, `agency/config.py`, `google_ads_connect.py`, and `--demo`.

## 2. Automated Quality & Multi-Platform CI
- [ ] `python -m pytest -q` passes 100% on Windows, macOS, and Linux (Python 3.11 and 3.12) in `.github/workflows/ci.yml`.
- [ ] `package-and-docker` job builds `google_ads_mcp_server-*.whl` and `docker build` cleanly.
- [ ] `privacy-guard` job passes with zero forbidden files or credential patterns.

## 3. Desktop Pilot Sign-Off
- [ ] **Pilot 1 (Lead Gen — Search):** Complete `connect` → `doctor` → `leads-audit` profile → `audit_conversion_tracking` → `diagnose_delivery` → `detect_waste`; verify figures match Google Ads UI (same currency, time zone, complete days).
- [ ] **Pilot 2 (Ecommerce — PMax + Shopping):** Complete `ecommerce-audit` profile → `get_pmax_diagnostics` → `get_shopping_performance` → `get_budget_pacing`.
- [ ] **Pilot 3 (Agency — Multi-MCC / Multi-currency):** Verify `generate_agency_dashboard` separates currencies, `list_client_accounts` routes multiple MCCs/direct accounts, and `google-ads-mcp-server alerts` runs cleanly from CLI.
- [ ] **Desktop Client Matrix:** Verify `stdio` startup & tool calling in Claude Desktop (`.mcpb` / config JSON), Codex, Antigravity, Gemini CLI, and one documented desktop client configured with DeepSeek/Kimi.

## 4. Packaging & Distribution
- [ ] Bump version to `1.0.0` in `pyproject.toml` and update `CHANGELOG.md`.
- [ ] Publish `google-ads-mcp-server` to PyPI (`twine upload dist/*`) and verify `uvx google-ads-mcp-server --demo` on a clean machine.
- [ ] Publish multi-arch container image to GHCR (`ghcr.io/<owner>/google-ads-mcp-server:1.0.0`).
- [ ] Submit server listing to Official MCP Registry, Glama, and Smithery.

## 5. Versioning & Google Ads API Support Policy
- Follow **SemVer** (`MAJOR.MINOR.PATCH`).
- Track the [Google Ads API sunset calendar](https://developers.google.com/google-ads/api/docs/sunset-dates) and bump `GOOGLE_ADS_API_VERSION` default at least 60 days before version deprecation.
