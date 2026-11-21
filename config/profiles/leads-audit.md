# Lead Generation Audit Template

## Objective
Evaluate lead-generation performance with evidence before proposing any change to budgets, bids, keywords, or conversion settings.

## Required Audit Sequence
1. **Account context & currency:** Confirm customer ID, account currency, time zone, and exact date range. Never compare or sum across different currencies.
2. **Measurement integrity first (`audit_conversion_tracking` + `get_offline_conversion_diagnostics`):**
   - Run `audit_conversion_tracking` to map conversion ownership (account vs. MCC), account/campaign/custom goals, and exact effective actions guiding Smart Bidding.
   - Separate **primary conversions** (`conversions`) from **secondary conversions** (`all_conversions`).
   - Do **not** interpret `conversions / all_conversions < 30%` as proof of Consent Mode failure; explain it as a measurement-scope difference and verify primary vs. secondary actions.
   - Check Customer Data Terms, Enhanced Conversions for Leads readiness, and offline upload ingestion health (`get_offline_conversion_diagnostics`, including Data Manager API eligibility).
3. **Wasted spend & Search quality:**
   - Identify non-converting search terms exceeding `min_waste_spend` and group them by intent before suggesting negatives.
   - Check impression share lost to budget vs. rank and RSA Ad Strength.
4. **CPA & budget efficiency:**
   - Compare actual CPA against `target_cpa` from the profile parameters.
   - Flag campaigns spending without conversions and distinguish low sample size from confirmed waste.

## Output Format
1. **Reproducibility Header:** Profile ID, version, customer ID, currency, date range, and active parameters.
2. **Executive Summary (3–5 bullets):** Spend, leads (`conversions`), CPA vs. target, and primary risk.
3. **Findings Ranked by Impact (High / Medium / Low):** Separate *confirmed causes*, *hypotheses*, and *insufficient data*.
4. **Prioritized Next Actions (Read-Only Proposals):** Present concrete dry-run candidates without executing changes.
