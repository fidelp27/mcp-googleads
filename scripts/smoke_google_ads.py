"""Call the live Google Ads API to verify credentials and basic queries."""

import asyncio
import re

from google_ads_server import (
    get_account_currency,
    get_campaign_performance,
    list_accounts,
    server_status,
)


def require_success(name: str, result: str) -> str:
    if result.lower().startswith("error"):
        raise RuntimeError(f"{name} failed: {result}")
    return result


async def run() -> None:
    status = server_status()
    if not status["ready"]:
        raise RuntimeError(
            "Server is not ready. Configure GOOGLE_ADS_CREDENTIALS_PATH "
            "and GOOGLE_ADS_DEVELOPER_TOKEN in .env."
        )

    accounts = require_success("list_accounts", await list_accounts())
    match = re.search(r"Account ID:\s*(\d{10})", accounts)
    if not match:
        raise RuntimeError(f"No accessible Google Ads account was returned:\n{accounts}")

    customer_id = match.group(1)
    currency = require_success(
        "get_account_currency",
        await get_account_currency(customer_id),
    )
    campaigns = require_success(
        "get_campaign_performance",
        await get_campaign_performance(customer_id, days=7),
    )

    print(accounts)
    print(currency)
    print(campaigns)
    print("Google Ads smoke test completed successfully.")


if __name__ == "__main__":
    asyncio.run(run())
