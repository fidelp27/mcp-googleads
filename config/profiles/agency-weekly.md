# Agency Weekly Multi-Account Review Template

## Objective
Give account managers and marketing leads a fast, reproducible weekly status check across all managed accounts without mixing currencies or missing delivery/policy blockers.

## Required Review Sequence
1. **Portfolio overview (`generate_agency_dashboard` + `check_alerts`):**
   - Keep currencies strictly separated (`managed_totals_by_currency`).
   - Highlight triggered alerts with account currency and time zone.
2. **Pacing & delivery health (`get_budget_pacing` + `diagnose_delivery`):**
   - Flag accounts projected to overspend or underspend by >10%.
   - Surface any `confirmed_causes` (billing, disapproved ads/assets, budget caps) separately from `hypotheses`.
3. **Channel & measurement spot-checks:**
   - Run `audit_conversion_tracking` for any account showing sudden CPA/ROAS swings.
   - Check `get_pmax_diagnostics` / `get_shopping_performance` for ecommerce accounts and `detect_waste` for Search accounts.

## Output Format
1. **Reproducibility Header:** Profile ID, version, date range, and active thresholds.
2. **Account Health Matrix:** Status (Red / Yellow / Green), MTD pacing %, spend (in local currency), conversions, and top blocker.
3. **Confirmed Issues Requiring Action This Week:** Ordered by spend at risk.
4. **Client-Ready Dry-Run Proposals:** Never apply changes without explicit preview and approval.
