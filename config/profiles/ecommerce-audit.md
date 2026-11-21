# Ecommerce Performance Audit Template

## Objective
Audit revenue efficiency (ROAS, conversion value, spend pacing, and wasted traffic) across Search, Performance Max, and Shopping campaigns.

## Required Audit Sequence
1. **Account context & currency:** Confirm customer ID, currency, time zone, and date window. Flag recent days as subject to conversion lag.
2. **Revenue & conversion value tracking (`audit_conversion_tracking`):**
   - Run `audit_conversion_tracking` to verify tracking ownership, primary purchase conversion actions, dynamic value tracking, double-counting risks, and attribution model status.
   - Separate purchase (`conversions`) from micro-conversions (`all_conversions`, e.g., Add to Cart).
3. **Channel & campaign efficiency (`get_pmax_diagnostics` + `get_shopping_performance`):**
   - Compare campaign ROAS against `target_roas` from the profile parameters.
   - Run `get_pmax_diagnostics` to inspect asset group ad strength, `LOW`/disapproved assets, search themes, and mobile-app placement share.
   - Run `get_shopping_performance` to separate top-converting SKUs/brands from zero-conversion wasted products.
   - Highlight budget-constrained high-ROAS campaigns separately from inefficient spend.
4. **Traffic waste & creative health:**
   - Surface non-converting Search terms above `min_waste_spend` and low-CTR segments.

## Output Format
1. **Reproducibility Header:** Profile ID, version, customer ID, currency, date range, and effective parameters.
2. **Executive Summary:** Spend, revenue (`conversions_value`), blended ROAS vs. `target_roas`, and top scaling/saving opportunities.
3. **Findings Ranked by Impact:** Clearly distinguish confirmed evidence from hypotheses.
4. **Prioritized Recommendations:** Actionable proposals ready for review and dry-run validation.
