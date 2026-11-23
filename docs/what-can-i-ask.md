# What Can I Ask? — Example Prompts by Scope & Level

> **Español:** [¿Qué puedo preguntarle? — Prompts de ejemplo por alcance y nivel](es/que-puedo-preguntar.md)

This is a plain-language guide to **what you can actually ask the MCP** in your
AI client (Claude Desktop, Claude Code, Codex, Cursor, Gemini CLI, etc.) once it
is connected to a Google Ads account.

You don't need to be a marketer to use it. If you can describe what you want
to know ("is anything broken?", "where is my money going?", "am I spending too
fast?"), the MCP will route to the right tools.

Each section below matches a **preset** (`core`, `leads`, `ecommerce`,
`agency`, `full`) — the bundle of tools that gets loaded. Pick the one closest
to your situation; you can always switch later (see the [README](../README.md#3-tool-catalog--presets---preset----modules)).

---

## General-level questions (work on any preset)

These are the "I just want to understand what's happening" prompts. They work
even on the default `core` preset.

**Status & accounts**
- *"Is the server working? Which account am I connected to?"*
- *"List the Google Ads accounts I can see."*
- *"What currency and timezone is this account in?"*

**Big-picture performance**
- *"How did the account perform last month? Last 7 days?"*
- *"Show me the top 10 campaigns by spend and tell me which are profitable."*
- *"Compare this month vs. last month — what changed?"*
- *"Give me a daily trend of spend and conversions for the last 30 days."*
- *"Build me a client report in Markdown for the last 30 days."*

**Finding waste**
- *"Where am I wasting money?"*
- *"Which search terms are spending without converting?"*
- *"Which keywords have high cost and zero conversions?"*
- *"Are any campaigns capped by budget?"*

**Alerts & suggestions**
- *"Check my alerts — anything over budget, CPA, or waste thresholds?"*
- *"Give me an optimization plan — what should I fix first?"*

---

## `core` preset — Default (25 tools)

**Who it's for:** Anyone starting out. Account overview, reports, trends,
waste detection.

**What you can ask:**

| Question | What the MCP does |
| --- | --- |
| *"How much did I spend last week vs. the week before?"* | Period comparison with % deltas. |
| *"Show me each campaign's CTR, CPC, CPA and ROAS."* | Campaign performance table. |
| *"Which ad creatives are underperforming?"* | RSA headlines, descriptions, strength, approval status. |
| *"Are any image assets missing or disapproved?"* | Image asset inventory and usage. |
| *"Write me a monthly report for the client in HTML."* | Executive report with branding, deltas, top items. |
| *"What's my budget utilization by campaign?"* | Spend vs. configured budget, flags capped campaigns. |
| *"Who changed what last week?"* | Reads Google Ads change history. |
| *"Which bidding strategies are my campaigns on?"* | Portfolio + campaign bidding list. |
| *"Run this custom GAQL query: ..."* | Safe GAQL execution with row limits. |

---

## `leads` preset — Lead generation (54 tools)

**Who it's for:** B2B, services, anyone capturing leads through forms, calls,
or signups.

**Adds** diagnostics, measurement (conversion tracking), and search.

**What you can ask:**

- *"Why aren't my ads showing?"* → runs 360° delivery diagnosis (billing,
  budget, policy, impression share losses).
- *"Am I on pace to hit my monthly budget?"* → month-to-date pacing with
  projected end-of-month.
- *"Audit my conversion tracking — is anything broken or duplicated?"*
- *"Is Enhanced Conversions for Leads working?"*
- *"Are my offline conversion uploads succeeding?"*
- *"Show me my worst-performing search terms by spend."*
- *"Cluster my search terms into themes and suggest negatives."*
- *"How is Quality Score distributed across my keywords?"*
- *"Are any of my Responsive Search Ads rated POOR or AVERAGE?"*
- *"Am I losing impression share to budget or to rank?"*
- *"Which landing pages have the worst mobile experience?"*
- *"How is each demographic (age, gender, income) performing?"*
- *"Give me keyword ideas for 'dental clinic Madrid'."*
- *"What's the historical search volume and competition for these keywords?"*

---

## `ecommerce` preset — Online stores (47 tools)

**Who it's for:** Shopify, WooCommerce, PrestaShop, or anyone selling products
online with Performance Max and Shopping campaigns.

**Adds** diagnostics, measurement, PMax, and Shopping tools.

**What you can ask:**

- *"Run a full Performance Max audit on my campaigns."*
- *"Which PMax asset groups are weak or have disapproved assets?"*
- *"What search terms are triggering my PMax campaigns?"*
- *"Is PMax showing on mobile apps I don't want?"*
- *"Which SKUs are losing money?"*
- *"Show me ROAS by brand, by category, and by product."*
- *"Which products are winners and which are wasted spend?"*
- *"How does performance break down by device? By geography? By time of day?"*
- *"What Google Ads recommendations are pending for this account?"*
- *"Audit my conversion tracking — is purchase value being passed correctly?"*

---

## `agency` preset — Multi-account / MCC (71 tools)

**Who it's for:** Agencies, consultants, and in-house teams managing multiple
accounts under a Manager Account (MCC).

**Adds** diagnostics, measurement, search, PMax, and MCC orchestration.

**What you can ask:**

- *"List all client accounts under my MCC."*
- *"Build an agency dashboard across all my clients — separated by currency."*
- *"Run this GAQL query across all my client accounts in parallel."*
- *"Switch context to client X and audit their account."*
- *"Which clients need a weekly review this week?"*
- *"Show me pending approvals across all accounts."*
- *"Which approvals are about to expire?"*
- *"Export the audit log as CSV for compliance."*
- *"Load the `agency-weekly` analysis profile and run it on client Y."*

---

## `full` preset — Power user (74 read tools, or 142 with writes enabled)

**Who it's for:** Technical users who want everything available. Enables
Display, Demand Gen, Experiments, and (optionally) the 68 guarded write
tools.

**Adds** Display, Demand Gen, and (opt-in) the `operations` module.

**Read-only extras:**

- *"Audit my Display placements — which are wasting money on mobile apps?"*
- *"What's my video view rate and quartile funnel on Demand Gen?"*
- *"Which placements have zero conversions?"*
- *"List my active experiments and compare control vs. treatment."*

**With writes enabled** (`READ_ONLY_MODE=false` + explicit human approval on
every change):

- *"Add these 15 negative keywords to campaign X."* → generates a dry-run
  preview, waits for your confirmation.
- *"Pause the campaigns with CPA over $200 and zero conversions."* →
  dry-run first, then executes only after you confirm.
- *"Increase the budget of campaign Y by 20%."* → enforces economic caps
  (`MAX_DAILY_BUDGET_INCREASE_MICROS`, `MAX_BUDGET_INCREASE_PCT`).
- *"Upload these offline conversions from my CRM."*
- *"Revert the last campaign pause I approved."*

Every write goes through: **dry_run preview → approval_id → exact
confirmation phrase → execute**. No surprise changes.

---

## Analysis profiles — guided audits

Instead of stringing prompts together, load a **profile** and let the MCP
run a structured audit for you:

- *"Load the `leads-audit` profile and audit account 1234567890."*
- *"Load the `ecommerce-audit` profile on this account."*
- *"Load the `agency-weekly` profile for my MCC."*

Profiles are Markdown + TOML files packaged with the server. You can also
write your own in `GOOGLE_ADS_CONFIG_DIR/profiles/`.

---

## Try it without credentials (`--demo`)

Not ready to connect a real account? Register with `--demo` and everything
above works against two synthetic accounts:

- `1111111111` — EUR ecommerce (Europe/Madrid)
- `2222222222` — USD B2B leads (America/New_York)

```bash
google-ads-mcp-server register --client claude-desktop --demo
```

Then ask any of the questions above and see real tool output with fake data.

---

## Still not sure what to ask?

A good starting prompt on any preset is:

> *"Audit this Google Ads account and tell me the top 3 things I should
> fix, in order of impact."*

The MCP will figure out which tools to call, flag what it's confident
about vs. what needs more data, and come back with a prioritized list.
