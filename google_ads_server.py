from typing import Any, Dict, List, Optional, Union
import argparse
import re
from pydantic import Field
import os
import json
import requests
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
import logging

# MCP
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from agency.governance import audited_tool
from agency.config import get_settings
from agency.demo import DEMO_ACCOUNTS, DEMO_CUSTOMER_IDS, demo_search, is_demo_mode
from agency.modules import PRESETS, configure_tool_catalog, get_active_catalog_info

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('google_ads_server')

mcp = FastMCP(
    "google-ads-mcp",
    instructions=(
        "Analyze Google Ads accounts and campaigns. Start with server_status, "
        "then list_accounts, and use the account ID returned by that tool."
    ),
    stateless_http=True,
    json_response=True,
    dependencies=[
        "google-auth-oauthlib",
        "google-auth",
        "requests",
        "python-dotenv"
    ]
)

# Constants and configuration
SCOPES = ['https://www.googleapis.com/auth/adwords']

# Load environment variables
try:
    from dotenv import load_dotenv
    # Load from .env file if it exists
    if not os.environ.get("GOOGLE_ADS_MANAGED_PROFILE"):
        load_dotenv()
        logger.info("Environment variables loaded from .env file")
except ImportError:
    logger.warning("python-dotenv not installed, skipping .env file loading")

# Get credentials from environment variables
GOOGLE_ADS_CREDENTIALS_PATH = os.environ.get("GOOGLE_ADS_CREDENTIALS_PATH")
GOOGLE_ADS_DEVELOPER_TOKEN = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN")
GOOGLE_ADS_LOGIN_CUSTOMER_ID = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")
GOOGLE_ADS_AUTH_TYPE = os.environ.get("GOOGLE_ADS_AUTH_TYPE", "oauth")  # oauth or service_account
GOOGLE_ADS_TOKEN_PATH = os.environ.get("GOOGLE_ADS_TOKEN_PATH")
MANAGED_PROFILE = os.environ.get("GOOGLE_ADS_MANAGED_PROFILE")
MANAGED_CREDENTIALS: Optional[Credentials] = None
MANAGED_CREDENTIALS_LOCK = Lock()
HTTP_TIMEOUT_SECONDS = float(os.environ.get("GOOGLE_ADS_HTTP_TIMEOUT_SECONDS", "30"))
API_VERSION = os.environ.get("GOOGLE_ADS_API_VERSION", "v25")
READ_ONLY_TOOL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
LOCAL_WRITE_TOOL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)


class _DemoResponse:
    """Minimal requests.Response-compatible object for --demo mode."""

    def __init__(self, status_code: int, payload: Dict[str, Any]) -> None:
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)
        self.content = self.text.encode("utf-8")
        self.headers: Dict[str, str] = {"content-type": "application/json"}

    def json(self) -> Dict[str, Any]:
        return self._payload


def http_get(url: str, **kwargs):
    """Make a bounded HTTP GET request to avoid hanging MCP tool calls."""
    kwargs.setdefault("timeout", HTTP_TIMEOUT_SECONDS)
    return requests.get(url, **kwargs)


def http_post(url: str, **kwargs):
    """Make a bounded HTTP POST request to avoid hanging MCP tool calls."""
    if is_demo_mode():
        match = re.search(r"/customers/(\d{10})/googleAds:search\b", url)
        if match:
            cid = match.group(1)
            query = (kwargs.get("json") or {}).get("query", "")
            return _DemoResponse(200, {"results": demo_search(cid, query)})
    kwargs.setdefault("timeout", HTTP_TIMEOUT_SECONDS)
    return requests.post(url, **kwargs)


@mcp.tool(annotations=READ_ONLY_TOOL)
@audited_tool
def server_status() -> Dict[str, Any]:
    """Check server readiness without exposing credential values or secrets."""
    if is_demo_mode():
        agency_settings = get_settings()
        return {
            "server": "google-ads-mcp",
            "package": "google-ads-mcp-server",
            "ready": True,
            "demo_mode": True,
            "demo_accounts": [
                {
                    "customer_id": cid,
                    "name": meta["name"],
                    "currency_code": meta["currency_code"],
                    "time_zone": meta["time_zone"],
                }
                for cid, meta in DEMO_ACCOUNTS.items()
            ],
            "auth_type": "demo",
            "credentials_configured": True,
            "developer_token_configured": False,
            "login_customer_configured": False,
            "oauth_token_present": None,
            "google_ads_api_version": API_VERSION,
            "agency": {
                "operator_configured": True,
                "read_only_mode": agency_settings.read_only_mode,
                "allowed_customer_count": len(agency_settings.allowed_customer_ids),
                "blocked_tool_count": len(agency_settings.blocked_tools),
                "client_approval_required": agency_settings.client_approval_required,
                "warnings": [
                    "Running in --demo mode with synthetic data (no Google Ads API calls or credentials required)."
                ],
            },
            "catalog": get_active_catalog_info(mcp),
            "issues": [],
        }
    issues = []
    auth_type = GOOGLE_ADS_AUTH_TYPE.strip().lower()
    credentials_path = (
        Path(GOOGLE_ADS_CREDENTIALS_PATH).expanduser()
        if GOOGLE_ADS_CREDENTIALS_PATH
        else None
    )

    if auth_type not in {"oauth", "service_account"}:
        issues.append("GOOGLE_ADS_AUTH_TYPE must be oauth or service_account")
    if MANAGED_PROFILE:
        if not MANAGED_CREDENTIALS or not MANAGED_CREDENTIALS.refresh_token:
            issues.append("Google connection must be renewed with google-ads-mcp-connect connect")
    elif not credentials_path:
        issues.append("GOOGLE_ADS_CREDENTIALS_PATH is not configured")
    elif not credentials_path.exists():
        issues.append("The configured Google Ads credentials file does not exist")
    if not re.fullmatch(r"v\d+", API_VERSION):
        issues.append("GOOGLE_ADS_API_VERSION must use a value such as v25")

    oauth_token_present = None
    if MANAGED_PROFILE:
        oauth_token_present = not issues
    elif auth_type == "oauth" and credentials_path and credentials_path.exists():
        token_path = Path(GOOGLE_ADS_TOKEN_PATH).expanduser() if GOOGLE_ADS_TOKEN_PATH else None
        try:
            credentials_data = json.loads(credentials_path.read_text(encoding="utf-8"))
            is_client_config = "installed" in credentials_data or "web" in credentials_data
            oauth_token_present = not is_client_config or bool(token_path and token_path.exists())
            if is_client_config and not oauth_token_present:
                issues.append("OAuth authorization is required; run scripts/authorize_oauth.py")
        except (OSError, json.JSONDecodeError):
            issues.append("The OAuth credentials file is not valid JSON")

    agency_settings = get_settings()
    agency_warnings = []
    if not agency_settings.read_only_mode and not agency_settings.allowed_customer_ids:
        agency_warnings.append(
            "Write mode is enabled without ALLOWED_CUSTOMER_IDS"
        )
    if agency_settings.operator_name == "unknown":
        agency_warnings.append("OPERATOR_NAME is not configured")

    return {
        "server": "google-ads-mcp",
        "ready": not issues,
        "auth_type": auth_type,
        "credentials_configured": oauth_token_present if MANAGED_PROFILE else bool(credentials_path and credentials_path.exists()),
        "developer_token_configured": bool(GOOGLE_ADS_DEVELOPER_TOKEN),
        "login_customer_configured": bool(GOOGLE_ADS_LOGIN_CUSTOMER_ID),
        "oauth_token_present": oauth_token_present,
        "google_ads_api_version": API_VERSION,
        "agency": {
            "operator_configured": agency_settings.operator_name != "unknown",
            "read_only_mode": agency_settings.read_only_mode,
            "allowed_customer_count": len(agency_settings.allowed_customer_ids),
            "blocked_tool_count": len(agency_settings.blocked_tools),
            "client_approval_required": agency_settings.client_approval_required,
            "warnings": agency_warnings,
        },
        "catalog": get_active_catalog_info(mcp),
        "issues": issues,
    }

def format_customer_id(customer_id: str) -> str:
    """Return a validated 10-digit Google Ads customer ID."""
    digits = "".join(char for char in str(customer_id) if char.isdigit())
    if not digits or len(digits) > 10:
        raise ValueError("customer_id must contain at most 10 digits")
    return digits.zfill(10)


def date_range_clause(days: int) -> str:
    """Build a valid GAQL date clause for any supported lookback period."""
    if not 1 <= days <= 365:
        raise ValueError("days must be between 1 and 365")
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=days - 1)
    return f"segments.date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'"


def validate_positive_integer(value: Union[str, int], field_name: str) -> int:
    """Validate integer IDs and limits before interpolating them into GAQL."""
    text = str(value).strip()
    if not re.fullmatch(r"[1-9]\d*", text):
        raise ValueError(f"{field_name} must be a positive integer")
    return int(text)

def get_credentials():
    """
    Get and refresh OAuth credentials or service account credentials based on the auth type.

    This function supports two authentication methods:
    1. OAuth 2.0 (User Authentication) - For individual users or desktop applications
    2. Service Account (Server-to-Server Authentication) - For automated systems

    Returns:
        Valid credentials object to use with Google Ads API
    """
    if is_demo_mode():
        return None
    if MANAGED_PROFILE:
        return get_oauth_credentials()
    if not GOOGLE_ADS_CREDENTIALS_PATH:
        raise ValueError("GOOGLE_ADS_CREDENTIALS_PATH environment variable not set")

    auth_type = GOOGLE_ADS_AUTH_TYPE.lower()
    logger.info(f"Using authentication type: {auth_type}")

    # Service Account authentication
    if auth_type == "service_account":
        try:
            return get_service_account_credentials()
        except Exception as e:
            logger.error(f"Error with service account authentication: {str(e)}")
            raise

    # OAuth 2.0 authentication (default)
    return get_oauth_credentials()

def get_service_account_credentials():
    """Get credentials using a service account key file."""
    logger.info(f"Loading service account credentials from {GOOGLE_ADS_CREDENTIALS_PATH}")

    if not os.path.exists(GOOGLE_ADS_CREDENTIALS_PATH):
        raise FileNotFoundError(f"Service account key file not found at {GOOGLE_ADS_CREDENTIALS_PATH}")

    try:
        credentials = service_account.Credentials.from_service_account_file(
            GOOGLE_ADS_CREDENTIALS_PATH,
            scopes=SCOPES
        )

        # Check if impersonation is required
        impersonation_email = os.environ.get("GOOGLE_ADS_IMPERSONATION_EMAIL")
        if impersonation_email:
            logger.info(f"Impersonating user: {impersonation_email}")
            credentials = credentials.with_subject(impersonation_email)

        return credentials

    except Exception as e:
        logger.error(f"Error loading service account credentials: {str(e)}")
        raise


def refresh_credentials_with_retry(creds, attempts: int = 3):
    """Refresh OAuth credentials with bounded exponential backoff."""
    last_error = None
    for attempt in range(attempts):
        try:
            creds.refresh(Request())
            if getattr(creds, "expiry", None):
                remaining = creds.expiry - datetime.utcnow()
                if remaining <= timedelta(minutes=10):
                    logger.warning("OAuth access token expires in less than 10 minutes")
            return creds
        except RefreshError as error:
            last_error = error
            logger.warning(
                "OAuth refresh attempt %s/%s failed: %s",
                attempt + 1,
                attempts,
                error,
            )
            if attempt < attempts - 1:
                time.sleep(2**attempt)
        except Exception:
            logger.exception("Unexpected OAuth refresh failure")
            raise
    raise RefreshError(f"OAuth token refresh failed after {attempts} attempts: {last_error}")

def get_oauth_credentials():
    """Get and refresh OAuth user credentials."""
    if MANAGED_PROFILE:
        with MANAGED_CREDENTIALS_LOCK:
            credentials = MANAGED_CREDENTIALS
            if not credentials or not credentials.refresh_token:
                raise ValueError("Google authorization is missing. Run google-ads-mcp-connect connect.")
            if not credentials.valid:
                try:
                    credentials.refresh(Request())
                except RefreshError:
                    raise ValueError("Google authorization could not be refreshed. Run google-ads-mcp-connect connect again.") from None
            return credentials
    creds = None
    client_config = None
    credentials_path = Path(GOOGLE_ADS_CREDENTIALS_PATH).expanduser()
    token_path = Path(GOOGLE_ADS_TOKEN_PATH).expanduser() if GOOGLE_ADS_TOKEN_PATH else None

    if credentials_path.is_dir():
        token_path = token_path or credentials_path / "google_ads_token.json"
    elif credentials_path.exists():
        try:
            logger.info("Loading OAuth configuration from %s", credentials_path)
            with credentials_path.open("r", encoding="utf-8") as f:
                creds_data = json.load(f)
                if "installed" in creds_data or "web" in creds_data:
                    client_config = creds_data
                    token_path = token_path or credentials_path.with_name("google_ads_token.json")
                else:
                    creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
                    token_path = token_path or credentials_path
        except json.JSONDecodeError:
            raise ValueError(f"Invalid JSON in OAuth credentials file: {credentials_path}")
        except Exception as e:
            logger.warning(f"Error loading credentials: {str(e)}")
            creds = None
    else:
        token_path = token_path or credentials_path

    if token_path and token_path.exists() and token_path != credentials_path:
        logger.info("Loading OAuth token from %s", token_path)
        with token_path.open("r", encoding="utf-8") as f:
            creds = Credentials.from_authorized_user_info(json.load(f), SCOPES)

    # If credentials don't exist or are invalid, get new ones
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                logger.info("Refreshing expired token")
                refresh_credentials_with_retry(creds)
                logger.info("Token successfully refreshed")
            except RefreshError as e:
                logger.warning(f"Error refreshing token: {str(e)}, will try to get new token")
                creds = None
            except Exception as e:
                logger.error(f"Unexpected error refreshing token: {str(e)}")
                raise

        # If we need new credentials
        if not creds:
            # If no client_config is defined yet, create one from environment variables
            if not client_config:
                logger.info("Creating OAuth client config from environment variables")
                client_id = os.environ.get("GOOGLE_ADS_CLIENT_ID")
                client_secret = os.environ.get("GOOGLE_ADS_CLIENT_SECRET")

                if not client_id or not client_secret:
                    raise ValueError("GOOGLE_ADS_CLIENT_ID and GOOGLE_ADS_CLIENT_SECRET must be set if no client config file exists")

                client_config = {
                    "installed": {
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"]
                    }
                }

            # Run the OAuth flow
            logger.info("Starting OAuth authentication flow")
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            creds = flow.run_local_server(port=0)
            logger.info("OAuth flow completed successfully")

        # Save the refreshed/new credentials
        try:
            if token_path is None:
                raise ValueError("No OAuth token path is configured")
            logger.info("Saving credentials to %s", token_path)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            with token_path.open("w", encoding="utf-8") as f:
                f.write(creds.to_json())
        except Exception as e:
            logger.warning(f"Could not save credentials: {str(e)}")

    return creds

def get_headers(creds):
    """Get headers for Google Ads API requests."""
    if is_demo_mode():
        return {
            "Authorization": "Bearer demo-mode",
            "content-type": "application/json",
        }
    # Handle different credential types
    if isinstance(creds, service_account.Credentials):
        # For service account, we need to get a new bearer token
        auth_req = Request()
        creds.refresh(auth_req)
        token = creds.token
    else:
        # For OAuth credentials, check if token needs refresh
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    logger.info("Refreshing expired OAuth token in get_headers")
                    refresh_credentials_with_retry(creds)
                    logger.info("Token successfully refreshed in get_headers")
                except RefreshError as e:
                    logger.error(f"Error refreshing token in get_headers: {str(e)}")
                    raise ValueError(f"Failed to refresh OAuth token: {str(e)}")
                except Exception as e:
                    logger.error(f"Unexpected error refreshing token in get_headers: {str(e)}")
                    raise
            else:
                raise ValueError("OAuth credentials are invalid and cannot be refreshed")

        token = creds.token

    headers = {
        'Authorization': f'Bearer {token}',
        'content-type': 'application/json'
    }
    if GOOGLE_ADS_DEVELOPER_TOKEN:
        headers['developer-token'] = GOOGLE_ADS_DEVELOPER_TOKEN

    if GOOGLE_ADS_LOGIN_CUSTOMER_ID:
        headers['login-customer-id'] = format_customer_id(GOOGLE_ADS_LOGIN_CUSTOMER_ID)

    return headers

@mcp.tool(annotations=READ_ONLY_TOOL)
@audited_tool
async def list_accounts() -> str:
    """
    Lists all accessible Google Ads accounts.

    This is typically the first command you should run to identify which accounts
    you have access to. The returned account IDs can be used in subsequent commands.

    Returns:
        A formatted list of all Google Ads accounts accessible with your credentials
    """
    if is_demo_mode():
        entries = [
            f"Account ID: {cid} — {meta['name']} ({meta['currency_code']}, {meta['time_zone']})"
            for cid, meta in DEMO_ACCOUNTS.items()
        ]
        return "Demo Google Ads accounts (synthetic read-only data):\n" + "\n".join(entries)
    if MANAGED_PROFILE:
        entries = [f"Account ID: {cid}" for cid in sorted(get_settings().allowed_customer_ids)]
        return "Connected Google Ads profile:\n" + "\n".join(entries)
    try:
        creds = get_credentials()
        headers = get_headers(creds)

        url = f"https://googleads.googleapis.com/{API_VERSION}/customers:listAccessibleCustomers"
        response = http_get(url, headers=headers)

        if response.status_code != 200:
            return f"Error accessing accounts: {response.text}"

        customers = response.json()
        if not customers.get('resourceNames'):
            return "No accessible accounts found."

        # Format the results
        result_lines = ["Accessible Google Ads Accounts:"]
        result_lines.append("-" * 50)

        for resource_name in customers['resourceNames']:
            customer_id = resource_name.split('/')[-1]
            formatted_id = format_customer_id(customer_id)
            result_lines.append(f"Account ID: {formatted_id}")

        return "\n".join(result_lines)

    except Exception as e:
        return f"Error listing accounts: {str(e)}"

@mcp.tool(annotations=READ_ONLY_TOOL)
@audited_tool
async def execute_gaql_query(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'"),
    query: str = Field(description="Valid GAQL query string following Google Ads Query Language syntax")
) -> str:
    """
    Execute a custom GAQL (Google Ads Query Language) query.

    This tool allows you to run any valid GAQL query against the Google Ads API.

    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        query: The GAQL query to execute (must follow GAQL syntax)

    Returns:
        Formatted query results or error message

    Example:
        customer_id: "1234567890"
        query: "SELECT campaign.id, campaign.name FROM campaign LIMIT 10"
    """
    try:
        creds = get_credentials()
        headers = get_headers(creds)

        formatted_customer_id = format_customer_id(customer_id)
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"

        payload = {"query": query}
        response = http_post(url, headers=headers, json=payload)

        if response.status_code != 200:
            return f"Error executing query: {response.text}"

        results = response.json()
        if not results.get('results'):
            return "No results found for the query."

        # Format the results as a table
        result_lines = [f"Query Results for Account {formatted_customer_id}:"]
        result_lines.append("-" * 80)

        # Get field names from the first result
        fields = []
        first_result = results['results'][0]
        for key in first_result:
            if isinstance(first_result[key], dict):
                for subkey in first_result[key]:
                    fields.append(f"{key}.{subkey}")
            else:
                fields.append(key)

        # Add header
        result_lines.append(" | ".join(fields))
        result_lines.append("-" * 80)

        # Add data rows
        for result in results['results']:
            row_data = []
            for field in fields:
                if "." in field:
                    parent, child = field.split(".")
                    value = str(result.get(parent, {}).get(child, ""))
                else:
                    value = str(result.get(field, ""))
                row_data.append(value)
            result_lines.append(" | ".join(row_data))

        return "\n".join(result_lines)

    except Exception as e:
        return f"Error executing GAQL query: {str(e)}"

@mcp.tool(annotations=READ_ONLY_TOOL)
@audited_tool
async def get_campaign_performance(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'"),
    days: int = Field(default=30, description="Number of days to look back (7, 30, 90, etc.)")
) -> str:
    """
    Get campaign performance metrics for the specified time period.

    RECOMMENDED WORKFLOW:
    1. First run list_accounts() to get available account IDs
    2. Then run get_account_currency() to see what currency the account uses
    3. Finally run this command to get campaign performance

    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        days: Number of days to look back (default: 30)

    Returns:
        Formatted table of campaign performance data

    Note:
        Cost values are in micros (millionths) of the account currency
        (e.g., 1000000 = 1 USD in a USD account)

    Example:
        customer_id: "1234567890"
        days: 14
    """
    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.average_cpc
        FROM campaign
        WHERE {date_range_clause(days)}
        ORDER BY metrics.cost_micros DESC
        LIMIT 50
    """

    return await execute_gaql_query(customer_id, query)

@mcp.tool(annotations=READ_ONLY_TOOL)
@audited_tool
async def get_ad_performance(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'"),
    days: int = Field(default=30, description="Number of days to look back (7, 30, 90, etc.)")
) -> str:
    """
    Get ad performance metrics for the specified time period.

    RECOMMENDED WORKFLOW:
    1. First run list_accounts() to get available account IDs
    2. Then run get_account_currency() to see what currency the account uses
    3. Finally run this command to get ad performance

    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        days: Number of days to look back (default: 30)

    Returns:
        Formatted table of ad performance data

    Note:
        Cost values are in micros (millionths) of the account currency
        (e.g., 1000000 = 1 USD in a USD account)

    Example:
        customer_id: "1234567890"
        days: 14
    """
    query = f"""
        SELECT
            ad_group_ad.ad.id,
            ad_group_ad.ad.name,
            ad_group_ad.status,
            campaign.name,
            ad_group.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM ad_group_ad
        WHERE {date_range_clause(days)}
        ORDER BY metrics.impressions DESC
        LIMIT 50
    """

    return await execute_gaql_query(customer_id, query)

@mcp.tool(annotations=READ_ONLY_TOOL)
@audited_tool
async def run_gaql(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'"),
    query: str = Field(description="Valid GAQL query string following Google Ads Query Language syntax"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'")
) -> str:
    """
    Execute any arbitrary GAQL (Google Ads Query Language) query with custom formatting options.

    This is the most powerful tool for custom Google Ads data queries.

    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        query: The GAQL query to execute (any valid GAQL query)
        format: Output format ("table", "json", or "csv")

    Returns:
        Query results in the requested format

    EXAMPLE QUERIES:

    1. Basic campaign metrics:
        SELECT
          campaign.name,
          metrics.clicks,
          metrics.impressions,
          metrics.cost_micros
        FROM campaign
        WHERE segments.date DURING LAST_7_DAYS

    2. Ad group performance:
        SELECT
          ad_group.name,
          metrics.conversions,
          metrics.cost_micros,
          campaign.name
        FROM ad_group
        WHERE metrics.clicks > 100

    3. Keyword analysis:
        SELECT
          ad_group_criterion.keyword.text,
          ad_group_criterion.keyword.match_type,
          metrics.ctr
        FROM keyword_view
        ORDER BY metrics.impressions DESC

    4. Get conversion data:
        SELECT
          campaign.name,
          metrics.conversions,
          metrics.conversions_value,
          metrics.cost_micros
        FROM campaign
        WHERE segments.date DURING LAST_30_DAYS

            Note:
        Cost values are in micros (millionths) of the account currency
        (e.g., 1000000 = 1 USD in a USD account)
    """
    try:
        creds = get_credentials()
        headers = get_headers(creds)

        formatted_customer_id = format_customer_id(customer_id)
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"

        payload = {"query": query}
        response = http_post(url, headers=headers, json=payload)

        if response.status_code != 200:
            return f"Error executing query: {response.text}"

        results = response.json()
        if not results.get('results'):
            return "No results found for the query."

        if format.lower() == "json":
            return json.dumps(results, indent=2)

        elif format.lower() == "csv":
            # Get field names from the first result
            fields = []
            first_result = results['results'][0]
            for key, value in first_result.items():
                if isinstance(value, dict):
                    for subkey in value:
                        fields.append(f"{key}.{subkey}")
                else:
                    fields.append(key)

            # Create CSV string
            csv_lines = [",".join(fields)]
            for result in results['results']:
                row_data = []
                for field in fields:
                    if "." in field:
                        parent, child = field.split(".")
                        value = str(result.get(parent, {}).get(child, "")).replace(",", ";")
                    else:
                        value = str(result.get(field, "")).replace(",", ";")
                    row_data.append(value)
                csv_lines.append(",".join(row_data))

            return "\n".join(csv_lines)

        else:  # default table format
            result_lines = [f"Query Results for Account {formatted_customer_id}:"]
            result_lines.append("-" * 100)

            # Get field names and maximum widths
            fields = []
            field_widths = {}
            first_result = results['results'][0]

            for key, value in first_result.items():
                if isinstance(value, dict):
                    for subkey in value:
                        field = f"{key}.{subkey}"
                        fields.append(field)
                        field_widths[field] = len(field)
                else:
                    fields.append(key)
                    field_widths[key] = len(key)

            # Calculate maximum field widths
            for result in results['results']:
                for field in fields:
                    if "." in field:
                        parent, child = field.split(".")
                        value = str(result.get(parent, {}).get(child, ""))
                    else:
                        value = str(result.get(field, ""))
                    field_widths[field] = max(field_widths[field], len(value))

            # Create formatted header
            header = " | ".join(f"{field:{field_widths[field]}}" for field in fields)
            result_lines.append(header)
            result_lines.append("-" * len(header))

            # Add data rows
            for result in results['results']:
                row_data = []
                for field in fields:
                    if "." in field:
                        parent, child = field.split(".")
                        value = str(result.get(parent, {}).get(child, ""))
                    else:
                        value = str(result.get(field, ""))
                    row_data.append(f"{value:{field_widths[field]}}")
                result_lines.append(" | ".join(row_data))

            return "\n".join(result_lines)

    except Exception as e:
        return f"Error executing GAQL query: {str(e)}"

@mcp.tool(annotations=READ_ONLY_TOOL)
@audited_tool
async def get_ad_creatives(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'")
) -> str:
    """
    Get ad creative details including headlines, descriptions, and URLs.

    This tool retrieves the actual ad content (headlines, descriptions)
    for review and analysis. Great for creative audits.

    RECOMMENDED WORKFLOW:
    1. First run list_accounts() to get available account IDs
    2. Then run this command with the desired account ID

    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)

    Returns:
        Formatted list of ad creative details

    Example:
        customer_id: "1234567890"
    """
    query = """
        SELECT
            ad_group_ad.ad.id,
            ad_group_ad.ad.name,
            ad_group_ad.ad.type,
            ad_group_ad.ad.final_urls,
            ad_group_ad.status,
            ad_group_ad.ad.responsive_search_ad.headlines,
            ad_group_ad.ad.responsive_search_ad.descriptions,
            ad_group.name,
            campaign.name
        FROM ad_group_ad
        WHERE ad_group_ad.status != 'REMOVED'
        ORDER BY campaign.name, ad_group.name
        LIMIT 50
    """

    try:
        creds = get_credentials()
        headers = get_headers(creds)

        formatted_customer_id = format_customer_id(customer_id)
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"

        payload = {"query": query}
        response = http_post(url, headers=headers, json=payload)

        if response.status_code != 200:
            return f"Error retrieving ad creatives: {response.text}"

        results = response.json()
        if not results.get('results'):
            return "No ad creatives found for this customer ID."

        # Format the results in a readable way
        output_lines = [f"Ad Creatives for Customer ID {formatted_customer_id}:"]
        output_lines.append("=" * 80)

        for i, result in enumerate(results['results'], 1):
            ad = result.get('adGroupAd', {}).get('ad', {})
            ad_group = result.get('adGroup', {})
            campaign = result.get('campaign', {})

            output_lines.append(f"\n{i}. Campaign: {campaign.get('name', 'N/A')}")
            output_lines.append(f"   Ad Group: {ad_group.get('name', 'N/A')}")
            output_lines.append(f"   Ad ID: {ad.get('id', 'N/A')}")
            output_lines.append(f"   Ad Name: {ad.get('name', 'N/A')}")
            output_lines.append(f"   Status: {result.get('adGroupAd', {}).get('status', 'N/A')}")
            output_lines.append(f"   Type: {ad.get('type', 'N/A')}")

            # Handle Responsive Search Ads
            rsa = ad.get('responsiveSearchAd', {})
            if rsa:
                if 'headlines' in rsa:
                    output_lines.append("   Headlines:")
                    for headline in rsa['headlines']:
                        output_lines.append(f"     - {headline.get('text', 'N/A')}")

                if 'descriptions' in rsa:
                    output_lines.append("   Descriptions:")
                    for desc in rsa['descriptions']:
                        output_lines.append(f"     - {desc.get('text', 'N/A')}")

            # Handle Final URLs
            final_urls = ad.get('finalUrls', [])
            if final_urls:
                output_lines.append(f"   Final URLs: {', '.join(final_urls)}")

            output_lines.append("-" * 80)

        return "\n".join(output_lines)

    except Exception as e:
        return f"Error retrieving ad creatives: {str(e)}"

@mcp.tool(annotations=READ_ONLY_TOOL)
@audited_tool
async def get_account_currency(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'")
) -> str:
    """
    Retrieve the default currency code used by the Google Ads account.

    IMPORTANT: Run this first before analyzing cost data to understand which currency
    the account uses. Cost values are always displayed in the account's currency.

    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)

    Returns:
        The account's default currency code (e.g., 'USD', 'EUR', 'GBP')

    Example:
        customer_id: "1234567890"
    """
    query = """
        SELECT
            customer.id,
            customer.currency_code
        FROM customer
        LIMIT 1
    """

    try:
        creds = get_credentials()

        # Force refresh if needed
        if not creds.valid:
            logger.info("Credentials not valid, attempting refresh...")
            if hasattr(creds, 'refresh_token') and creds.refresh_token:
                creds.refresh(Request())
                logger.info("Credentials refreshed successfully")
            else:
                raise ValueError("Invalid credentials and no refresh token available")

        headers = get_headers(creds)

        formatted_customer_id = format_customer_id(customer_id)
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"

        payload = {"query": query}
        response = http_post(url, headers=headers, json=payload)

        if response.status_code != 200:
            return f"Error retrieving account currency: {response.text}"

        results = response.json()
        if not results.get('results'):
            return "No account information found for this customer ID."

        # Extract the currency code from the results
        customer = results['results'][0].get('customer', {})
        currency_code = customer.get('currencyCode', 'Not specified')

        return f"Account {formatted_customer_id} uses currency: {currency_code}"

    except Exception as e:
        logger.error(f"Error retrieving account currency: {str(e)}")
        return f"Error retrieving account currency: {str(e)}"

@mcp.resource("gaql://reference")
def gaql_reference() -> str:
    """Google Ads Query Language (GAQL) reference documentation."""
    return """
    # Google Ads Query Language (GAQL) Reference

    GAQL is similar to SQL but with specific syntax for Google Ads. Here's a quick reference:

    ## Basic Query Structure
    ```
    SELECT field1, field2, ...
    FROM resource_type
    WHERE condition
    ORDER BY field [ASC|DESC]
    LIMIT n
    ```

    ## Common Field Types

    ### Resource Fields
    - campaign.id, campaign.name, campaign.status
    - ad_group.id, ad_group.name, ad_group.status
    - ad_group_ad.ad.id, ad_group_ad.ad.final_urls
    - ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type

    ### Metric Fields
    - metrics.impressions
    - metrics.clicks
    - metrics.cost_micros
    - metrics.conversions
    - metrics.ctr
    - metrics.average_cpc

    ### Segment Fields
    - segments.date
    - segments.device
    - segments.day_of_week

    ## Common WHERE Clauses

    ### Date Ranges
    - WHERE segments.date DURING LAST_7_DAYS
    - WHERE segments.date DURING LAST_30_DAYS
    - WHERE segments.date BETWEEN '2023-01-01' AND '2023-01-31'

    ### Filtering
    - WHERE campaign.status = 'ENABLED'
    - WHERE metrics.clicks > 100
    - WHERE campaign.name LIKE '%Brand%'

    ## Tips
    - Always check account currency before analyzing cost data
    - Cost values are in micros (millionths): 1000000 = 1 unit of currency
    - Use LIMIT to avoid large result sets
    """

@mcp.prompt("google_ads_workflow")
def google_ads_workflow() -> str:
    """Provides guidance on the recommended workflow for using Google Ads tools."""
    return """
    I'll help you analyze your Google Ads account data. Here's the recommended workflow:

    1. First, let's list all the accounts you have access to:
       - Run the `list_accounts()` tool to get available account IDs

    2. Before analyzing cost data, let's check which currency the account uses:
       - Run `get_account_currency(customer_id="ACCOUNT_ID")` with your selected account

    3. Now we can explore the account data:
       - For campaign performance: `get_campaign_performance(customer_id="ACCOUNT_ID", days=30)`
       - For ad performance: `get_ad_performance(customer_id="ACCOUNT_ID", days=30)`
       - For ad creative review: `get_ad_creatives(customer_id="ACCOUNT_ID")`

    4. For custom queries, use the GAQL query tool:
       - `run_gaql(customer_id="ACCOUNT_ID", query="YOUR_QUERY", format="table")`

    5. Let me know if you have specific questions about:
       - Campaign performance
       - Ad performance
       - Keywords
       - Budgets
       - Conversions

    Important: Always provide the customer_id as a string.
    For example: customer_id="1234567890"
    """

@mcp.prompt("gaql_help")
def gaql_help() -> str:
    """Provides assistance for writing GAQL queries."""
    return """
    I'll help you write a Google Ads Query Language (GAQL) query. Here are some examples to get you started:

    ## Get campaign performance last 30 days
    ```
    SELECT
      campaign.id,
      campaign.name,
      campaign.status,
      metrics.impressions,
      metrics.clicks,
      metrics.cost_micros,
      metrics.conversions
    FROM campaign
    WHERE segments.date DURING LAST_30_DAYS
    ORDER BY metrics.cost_micros DESC
    ```

    ## Get keyword performance
    ```
    SELECT
      ad_group_criterion.keyword.text,
      ad_group_criterion.keyword.match_type,
      metrics.impressions,
      metrics.clicks,
      metrics.cost_micros,
      metrics.conversions
    FROM keyword_view
    WHERE segments.date DURING LAST_30_DAYS
    ORDER BY metrics.clicks DESC
    ```

    ## Get ads with poor performance
    ```
    SELECT
      ad_group_ad.ad.id,
      ad_group_ad.ad.name,
      campaign.name,
      ad_group.name,
      metrics.impressions,
      metrics.clicks,
      metrics.conversions
    FROM ad_group_ad
    WHERE
      segments.date DURING LAST_30_DAYS
      AND metrics.impressions > 1000
      AND metrics.ctr < 0.01
    ORDER BY metrics.impressions DESC
    ```

    Once you've chosen a query, use it with:
    ```
    run_gaql(customer_id="YOUR_ACCOUNT_ID", query="YOUR_QUERY_HERE")
    ```

    Remember:
    - Always provide the customer_id as a string
    - Cost values are in micros (1,000,000 = 1 unit of currency)
    - Use LIMIT to avoid large result sets
    - Check the account currency before analyzing cost data
    """

@mcp.tool(annotations=READ_ONLY_TOOL)
@audited_tool
async def get_image_assets(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'"),
    limit: int = Field(default=50, description="Maximum number of image assets to return")
) -> str:
    """
    Retrieve all image assets in the account including their full-size URLs.

    This tool allows you to get details about image assets used in your Google Ads account,
    including the URLs to download the full-size images for further processing or analysis.

    RECOMMENDED WORKFLOW:
    1. First run list_accounts() to get available account IDs
    2. Then run this command with the desired account ID

    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        limit: Maximum number of image assets to return (default: 50)

    Returns:
        Formatted list of image assets with their download URLs

    Example:
        customer_id: "1234567890"
        limit: 100
    """
    validated_limit = validate_positive_integer(limit, "limit")
    if validated_limit > 500:
        raise ValueError("limit must not exceed 500")
    query = f"""
        SELECT
            asset.id,
            asset.name,
            asset.type,
            asset.image_asset.full_size.url,
            asset.image_asset.full_size.height_pixels,
            asset.image_asset.full_size.width_pixels,
            asset.image_asset.file_size
        FROM
            asset
        WHERE
            asset.type = 'IMAGE'
        LIMIT {validated_limit}
    """

    try:
        creds = get_credentials()
        headers = get_headers(creds)

        formatted_customer_id = format_customer_id(customer_id)
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"

        payload = {"query": query}
        response = http_post(url, headers=headers, json=payload)

        if response.status_code != 200:
            return f"Error retrieving image assets: {response.text}"

        results = response.json()
        if not results.get('results'):
            return "No image assets found for this customer ID."

        # Format the results in a readable way
        output_lines = [f"Image Assets for Customer ID {formatted_customer_id}:"]
        output_lines.append("=" * 80)

        for i, result in enumerate(results['results'], 1):
            asset = result.get('asset', {})
            image_asset = asset.get('imageAsset', {})
            full_size = image_asset.get('fullSize', {})

            output_lines.append(f"\n{i}. Asset ID: {asset.get('id', 'N/A')}")
            output_lines.append(f"   Name: {asset.get('name', 'N/A')}")

            if full_size:
                output_lines.append(f"   Image URL: {full_size.get('url', 'N/A')}")
                output_lines.append(f"   Dimensions: {full_size.get('widthPixels', 'N/A')} x {full_size.get('heightPixels', 'N/A')} px")

            file_size = image_asset.get('fileSize', 'N/A')
            if file_size != 'N/A':
                # Convert to KB for readability
                file_size_kb = int(file_size) / 1024
                output_lines.append(f"   File Size: {file_size_kb:.2f} KB")

            output_lines.append("-" * 80)

        return "\n".join(output_lines)

    except Exception as e:
        return f"Error retrieving image assets: {str(e)}"

@mcp.tool(annotations=LOCAL_WRITE_TOOL)
@audited_tool
async def download_image_asset(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'"),
    asset_id: str = Field(description="The ID of the image asset to download"),
    output_dir: str = Field(default="./ad_images", description="Directory to save the downloaded image")
) -> str:
    """
    Download a specific image asset from a Google Ads account.

    This tool allows you to download the full-size version of an image asset
    for further processing, analysis, or backup.

    RECOMMENDED WORKFLOW:
    1. First run list_accounts() to get available account IDs
    2. Then run get_image_assets() to get available image asset IDs
    3. Finally use this command to download specific images

    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        asset_id: The ID of the image asset to download
        output_dir: Directory where the image should be saved (default: ./ad_images)

    Returns:
        Status message indicating success or failure of the download

    Example:
        customer_id: "1234567890"
        asset_id: "12345"
        output_dir: "./my_ad_images"
    """
    validated_asset_id = validate_positive_integer(asset_id, "asset_id")
    query = f"""
        SELECT
            asset.id,
            asset.name,
            asset.image_asset.full_size.url
        FROM
            asset
        WHERE
            asset.type = 'IMAGE'
            AND asset.id = {validated_asset_id}
        LIMIT 1
    """

    try:
        creds = get_credentials()
        headers = get_headers(creds)

        formatted_customer_id = format_customer_id(customer_id)
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"

        payload = {"query": query}
        response = http_post(url, headers=headers, json=payload)

        if response.status_code != 200:
            return f"Error retrieving image asset: {response.text}"

        results = response.json()
        if not results.get('results'):
            return f"No image asset found with ID {validated_asset_id}"

        # Extract the image URL
        asset = results['results'][0].get('asset', {})
        image_url = asset.get('imageAsset', {}).get('fullSize', {}).get('url')
        asset_name = asset.get('name', f"image_{asset_id}")

        if not image_url:
            return f"No download URL found for image asset ID {asset_id}"

        # Validate and sanitize the output directory to prevent path traversal
        try:
            # Get the base directory (current working directory)
            from agency.config import resolve_worker_file

            base_dir = resolve_worker_file(Path(".")).resolve()
            # Resolve the output directory to an absolute path
            resolved_output_dir = resolve_worker_file(Path(output_dir)).resolve()

            # Ensure the resolved path is within or under the current working directory
            # This prevents path traversal attacks like "../../../etc"
            try:
                resolved_output_dir.relative_to(base_dir)
            except ValueError:
                # If the path is not relative to base_dir, use the default safe directory
                resolved_output_dir = base_dir / "ad_images"
                logger.warning(f"Invalid output directory '{output_dir}' - using default './ad_images'")

            # Create output directory if it doesn't exist
            resolved_output_dir.mkdir(parents=True, exist_ok=True)

        except Exception as e:
            return f"Error creating output directory: {str(e)}"

        # Download the image
        image_response = http_get(image_url)
        if image_response.status_code != 200:
            return f"Failed to download image: HTTP {image_response.status_code}"

        # Clean the filename to be safe for filesystem
        safe_name = ''.join(c for c in asset_name if c.isalnum() or c in ' ._-')
        filename = f"{asset_id}_{safe_name}.jpg"
        file_path = resolved_output_dir / filename

        # Save the image
        with open(file_path, 'wb') as f:
            f.write(image_response.content)

        return f"Successfully downloaded image asset {asset_id} to {file_path}"

    except Exception as e:
        return f"Error downloading image asset: {str(e)}"

@mcp.tool(annotations=READ_ONLY_TOOL)
@audited_tool
async def get_asset_usage(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'"),
    asset_id: str = Field(default=None, description="Optional: specific asset ID to look up (leave empty to get all image assets)"),
    asset_type: str = Field(default="IMAGE", description="Asset type to search for ('IMAGE', 'TEXT', 'VIDEO', etc.)")
) -> str:
    """
    Find where specific assets are being used in campaigns and ad groups.

    This tool helps you analyze how assets are linked to campaigns and ads across your account,
    which is useful for creative analysis and optimization.

    RECOMMENDED WORKFLOW:
    1. First run list_accounts() to get available account IDs
    2. Run get_image_assets() to see available assets
    3. Use this command to see where specific assets are used

    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        asset_id: Optional specific asset ID to look up (leave empty to get all assets of the specified type)
        asset_type: Type of asset to search for (default: 'IMAGE')

    Returns:
        Formatted report showing where assets are used in the account

    Example:
        customer_id: "1234567890"
        asset_id: "12345"
        asset_type: "IMAGE"
    """
    # Build the query based on whether a specific asset ID was provided
    normalized_asset_type = asset_type.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", normalized_asset_type):
        raise ValueError("asset_type must be a Google Ads enum name")
    where_clause = f"asset.type = '{normalized_asset_type}'"
    if asset_id:
        where_clause += f" AND asset.id = {validate_positive_integer(asset_id, 'asset_id')}"

    # First get the assets themselves
    assets_query = f"""
        SELECT
            asset.id,
            asset.name,
            asset.type
        FROM
            asset
        WHERE
            {where_clause}
        LIMIT 100
    """

    # Then get the associations between assets and campaigns/ad groups
    # Try using campaign_asset instead of asset_link
    associations_query = f"""
        SELECT
            campaign.id,
            campaign.name,
            asset.id,
            asset.name,
            asset.type
        FROM
            campaign_asset
        WHERE
            {where_clause}
        LIMIT 500
    """

    # Also try ad_group_asset for ad group level information
    ad_group_query = f"""
        SELECT
            ad_group.id,
            ad_group.name,
            asset.id,
            asset.name,
            asset.type
        FROM
            ad_group_asset
        WHERE
            {where_clause}
        LIMIT 500
    """

    try:
        creds = get_credentials()
        headers = get_headers(creds)

        formatted_customer_id = format_customer_id(customer_id)

        # First get the assets
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"
        payload = {"query": assets_query}
        assets_response = http_post(url, headers=headers, json=payload)

        if assets_response.status_code != 200:
            return f"Error retrieving assets: {assets_response.text}"

        assets_results = assets_response.json()
        if not assets_results.get('results'):
            return f"No {asset_type} assets found for this customer ID."

        # Now get the associations
        payload = {"query": associations_query}
        assoc_response = http_post(url, headers=headers, json=payload)

        if assoc_response.status_code != 200:
            return f"Error retrieving asset associations: {assoc_response.text}"

        assoc_results = assoc_response.json()

        payload = {"query": ad_group_query}
        ad_group_response = http_post(url, headers=headers, json=payload)
        if ad_group_response.status_code != 200:
            return f"Error retrieving ad group asset associations: {ad_group_response.text}"
        ad_group_results = ad_group_response.json()

        # Format the results in a readable way
        output_lines = [f"Asset Usage for Customer ID {formatted_customer_id}:"]
        output_lines.append("=" * 80)

        # Create a dictionary to organize asset usage by asset ID
        asset_usage = {}

        # Initialize the asset usage dictionary with basic asset info
        for result in assets_results.get('results', []):
            asset = result.get('asset', {})
            asset_id = asset.get('id')
            if asset_id:
                asset_usage[asset_id] = {
                    'name': asset.get('name', 'Unnamed asset'),
                    'type': asset.get('type', 'Unknown'),
                    'usage': []
                }

        # Add usage information from the associations
        for result in assoc_results.get('results', []):
            asset = result.get('asset', {})
            asset_id = asset.get('id')

            if asset_id and asset_id in asset_usage:
                campaign = result.get('campaign', {})
                ad_group = result.get('adGroup', {})
                usage_info = {
                    'campaign_id': campaign.get('id', 'N/A'),
                    'campaign_name': campaign.get('name', 'N/A'),
                    'ad_group_id': ad_group.get('id', 'N/A'),
                    'ad_group_name': ad_group.get('name', 'N/A'),
                }

                asset_usage[asset_id]['usage'].append(usage_info)

        for result in ad_group_results.get('results', []):
            asset = result.get('asset', {})
            result_asset_id = asset.get('id')
            if result_asset_id and result_asset_id in asset_usage:
                ad_group = result.get('adGroup', {})
                asset_usage[result_asset_id]['usage'].append(
                    {
                        'campaign_id': 'N/A',
                        'campaign_name': 'N/A',
                        'ad_group_id': ad_group.get('id', 'N/A'),
                        'ad_group_name': ad_group.get('name', 'N/A'),
                    }
                )

        # Format the output
        for asset_id, info in asset_usage.items():
            output_lines.append(f"\nAsset ID: {asset_id}")
            output_lines.append(f"Name: {info['name']}")
            output_lines.append(f"Type: {info['type']}")

            if info['usage']:
                output_lines.append("\nUsed in:")
                output_lines.append("-" * 60)
                output_lines.append(f"{'Campaign':<30} | {'Ad Group':<30}")
                output_lines.append("-" * 60)

                for usage in info['usage']:
                    campaign_str = f"{usage['campaign_name']} ({usage['campaign_id']})"
                    ad_group_str = f"{usage['ad_group_name']} ({usage['ad_group_id']})"

                    output_lines.append(f"{campaign_str[:30]:<30} | {ad_group_str[:30]:<30}")

            output_lines.append("=" * 80)

        return "\n".join(output_lines)

    except Exception as e:
        return f"Error retrieving asset usage: {str(e)}"

@mcp.tool(annotations=READ_ONLY_TOOL)
@audited_tool
async def analyze_image_assets(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'"),
    days: int = Field(default=30, description="Number of days to look back (7, 30, 90, etc.)")
) -> str:
    """
    Analyze image assets with their performance metrics across campaigns.

    This comprehensive tool helps you understand which image assets are performing well
    by showing metrics like impressions, clicks, and conversions for each image.

    RECOMMENDED WORKFLOW:
    1. First run list_accounts() to get available account IDs
    2. Then run get_account_currency() to see what currency the account uses
    3. Finally run this command to analyze image asset performance

    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        days: Number of days to look back (default: 30)

    Returns:
        Detailed report of image assets and their performance metrics

    Example:
        customer_id: "1234567890"
        days: 14
    """
    query = f"""
        SELECT
            asset.id,
            asset.name,
            asset.image_asset.full_size.url,
            asset.image_asset.full_size.width_pixels,
            asset.image_asset.full_size.height_pixels,
            campaign.name,
            metrics.impressions,
            metrics.clicks,
            metrics.conversions,
            metrics.cost_micros
        FROM
            campaign_asset
        WHERE
            asset.type = 'IMAGE'
            AND {date_range_clause(days)}
        ORDER BY
            metrics.impressions DESC
        LIMIT 200
    """

    try:
        creds = get_credentials()
        headers = get_headers(creds)

        formatted_customer_id = format_customer_id(customer_id)
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"

        payload = {"query": query}
        response = http_post(url, headers=headers, json=payload)

        if response.status_code != 200:
            return f"Error analyzing image assets: {response.text}"

        results = response.json()
        if not results.get('results'):
            return "No image asset performance data found for this customer ID and time period."

        # Group results by asset ID
        assets_data = {}
        for result in results.get('results', []):
            asset = result.get('asset', {})
            asset_id = asset.get('id')

            if asset_id not in assets_data:
                assets_data[asset_id] = {
                    'name': asset.get('name', f"Asset {asset_id}"),
                    'url': asset.get('imageAsset', {}).get('fullSize', {}).get('url', 'N/A'),
                    'dimensions': f"{asset.get('imageAsset', {}).get('fullSize', {}).get('widthPixels', 'N/A')} x {asset.get('imageAsset', {}).get('fullSize', {}).get('heightPixels', 'N/A')}",
                    'impressions': 0,
                    'clicks': 0,
                    'conversions': 0,
                    'cost_micros': 0,
                    'campaigns': set(),
                    'ad_groups': set()
                }

            # Aggregate metrics
            metrics = result.get('metrics', {})
            assets_data[asset_id]['impressions'] += int(metrics.get('impressions', 0))
            assets_data[asset_id]['clicks'] += int(metrics.get('clicks', 0))
            assets_data[asset_id]['conversions'] += float(metrics.get('conversions', 0))
            assets_data[asset_id]['cost_micros'] += int(metrics.get('costMicros', 0))

            # Add campaign and ad group info
            campaign = result.get('campaign', {})
            ad_group = result.get('adGroup', {})

            if campaign.get('name'):
                assets_data[asset_id]['campaigns'].add(campaign.get('name'))
            if ad_group.get('name'):
                assets_data[asset_id]['ad_groups'].add(ad_group.get('name'))

        # Format the results
        output_lines = [f"Image Asset Performance Analysis for Customer ID {formatted_customer_id} (Last {days} days):"]
        output_lines.append("=" * 100)

        # Sort assets by impressions (highest first)
        sorted_assets = sorted(assets_data.items(), key=lambda x: x[1]['impressions'], reverse=True)

        for asset_id, data in sorted_assets:
            output_lines.append(f"\nAsset ID: {asset_id}")
            output_lines.append(f"Name: {data['name']}")
            output_lines.append(f"Dimensions: {data['dimensions']}")

            # Calculate CTR if there are impressions
            ctr = (data['clicks'] / data['impressions'] * 100) if data['impressions'] > 0 else 0

            # Format metrics
            output_lines.append(f"\nPerformance Metrics:")
            output_lines.append(f"  Impressions: {data['impressions']:,}")
            output_lines.append(f"  Clicks: {data['clicks']:,}")
            output_lines.append(f"  CTR: {ctr:.2f}%")
            output_lines.append(f"  Conversions: {data['conversions']:.2f}")
            output_lines.append(f"  Cost (micros): {data['cost_micros']:,}")

            # Show where it's used
            output_lines.append(f"\nUsed in {len(data['campaigns'])} campaigns:")
            for campaign in list(data['campaigns'])[:5]:  # Show first 5 campaigns
                output_lines.append(f"  - {campaign}")
            if len(data['campaigns']) > 5:
                output_lines.append(f"  - ... and {len(data['campaigns']) - 5} more")

            # Add URL
            if data['url'] != 'N/A':
                output_lines.append(f"\nImage URL: {data['url']}")

            output_lines.append("-" * 100)

        return "\n".join(output_lines)

    except Exception as e:
        return f"Error analyzing image assets: {str(e)}"

@mcp.tool(annotations=READ_ONLY_TOOL)
@audited_tool
async def list_resources(
    customer_id: Optional[str] = Field(
        default=None,
        description="Deprecated and ignored; GoogleAdsFieldService does not require a customer ID",
    )
) -> str:
    """
    List valid resources that can be used in GAQL FROM clauses.

    This metadata comes from GoogleAdsFieldService and does not require a
    customer ID. The optional argument is retained for backward compatibility.
    """
    query = """
        SELECT
            name,
            category,
            data_type
        WHERE
            category = 'RESOURCE'
        ORDER BY
            name
        LIMIT 500
    """

    try:
        credentials = get_credentials()
        headers = get_headers(credentials)
        url = f"https://googleads.googleapis.com/{API_VERSION}/googleAdsFields:search"
        response = http_post(url, headers=headers, json={"query": query})
        if response.status_code != 200:
            return f"Error retrieving Google Ads resources: {response.text}"

        results = response.json().get("results", [])
        if not results:
            return "No Google Ads resources were returned."

        lines = [f"Google Ads resources available in {API_VERSION}:"]
        lines.extend(
            f"- {field.get('name', 'unknown')} ({field.get('dataType', 'unknown')})"
            for field in results
        )
        return "\n".join(lines)
    except Exception as error:
        return f"Error retrieving Google Ads resources: {error}"

from agency import register_agency_tools

register_agency_tools(mcp)


def apply_managed_profile(
    profile_path: Union[str, Path],
    credentials: Credentials,
    *,
    manager: str = "",
    api_version: Optional[str] = None,
) -> None:
    """Apply managed profile runtime state even if `google_ads_server` was already imported."""
    global MANAGED_PROFILE, MANAGED_CREDENTIALS
    global GOOGLE_ADS_LOGIN_CUSTOMER_ID, GOOGLE_ADS_DEVELOPER_TOKEN
    global GOOGLE_ADS_AUTH_TYPE, GOOGLE_ADS_CREDENTIALS_PATH, GOOGLE_ADS_TOKEN_PATH
    global API_VERSION

    MANAGED_PROFILE = str(profile_path)
    with MANAGED_CREDENTIALS_LOCK:
        MANAGED_CREDENTIALS = credentials
    GOOGLE_ADS_LOGIN_CUSTOMER_ID = manager or ""
    GOOGLE_ADS_DEVELOPER_TOKEN = ""
    GOOGLE_ADS_AUTH_TYPE = "oauth"
    GOOGLE_ADS_CREDENTIALS_PATH = None
    GOOGLE_ADS_TOKEN_PATH = None
    if api_version:
        API_VERSION = api_version

    # Re-sync agency path bindings if agency modules were imported before env was populated.
    import agency.analysis_profiles as _ap
    import agency.audit_retention as _ar
    import agency.config as _cfg
    import agency.governance as _gov
    import agency.locks as _locks
    import agency.runtime as _rt

    new_cfg_dir = Path(os.environ.get("GOOGLE_ADS_CONFIG_DIR", _cfg.CONFIG_DIR)).expanduser()
    new_data_dir = Path(os.environ.get("GOOGLE_ADS_DATA_DIR", _cfg.DATA_DIR)).expanduser()
    _cfg.CONFIG_DIR = new_cfg_dir
    _cfg.DATA_DIR = new_data_dir
    _ap.CONFIG_DIR = new_cfg_dir
    _gov.DATA_DIR = new_data_dir
    _gov.AUDIT_PATH = new_data_dir / "audit.jsonl"
    _gov.APPROVAL_PATH = new_data_dir / "pending_approvals.jsonl"
    _locks.DATA_DIR = new_data_dir
    _locks.LOCK_ROOT = new_data_dir / "locks"
    _ar.DATA_DIR = new_data_dir
    _rt.DATA_DIR = new_data_dir


def main(argv: Optional[List[str]] = None) -> None:
    """Run the server locally over stdio, remotely over Streamable HTTP, or delegate setup subcommands."""
    import sys

    raw_args = list(sys.argv[1:] if argv is None else argv)

    if raw_args and raw_args[0] == "alerts":
        from agency.cli_alerts import main as alerts_main

        alerts_main(raw_args[1:])
        return

    from google_ads_cli import _is_connect_argv

    if _is_connect_argv(raw_args):
        import google_ads_connect

        google_ads_connect.main(raw_args)
        return

    parser = argparse.ArgumentParser(description="Google Ads MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help="stdio for desktop clients; streamable-http for self-hosted remote clients",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MCP_HOST", "127.0.0.1"),
        help="HTTP bind host (ignored by stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", os.environ.get("MCP_PORT", "8000"))),
        help="HTTP bind port (ignored by stdio)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run in zero-credential read-only demo mode with realistic synthetic accounts",
    )
    parser.add_argument(
        "--preset",
        choices=tuple(sorted(PRESETS)),
        default=os.environ.get("GOOGLE_ADS_PRESET") or None,
        help="Preset bundle of tool modules (core, leads, ecommerce, agency, full)",
    )
    parser.add_argument(
        "--modules",
        default=os.environ.get("GOOGLE_ADS_MODULES") or None,
        help="Comma-separated tool modules to load (e.g. core,measurement,search)",
    )
    args = parser.parse_args(raw_args)

    if args.demo:
        os.environ["GOOGLE_ADS_DEMO_MODE"] = "true"
        os.environ["READ_ONLY_MODE"] = "true"
        os.environ["ALLOWED_CUSTOMER_IDS"] = ",".join(DEMO_CUSTOMER_IDS)
        logger.info("Starting Google Ads MCP server in --demo mode (read-only synthetic data)")

    configure_tool_catalog(mcp, modules=args.modules, preset=args.preset)

    if args.transport == "streamable-http":
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        allow_remote = os.environ.get("MCP_HTTP_ALLOW_REMOTE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if args.host not in loopback_hosts and not allow_remote:
            raise SystemExit(
                f"Refusing to bind Streamable HTTP on non-loopback host '{args.host}' without "
                "explicit remote protection. Use --host 127.0.0.1 locally, or place an authenticated "
                "HTTPS reverse proxy (with Origin validation) in front and set MCP_HTTP_ALLOW_REMOTE=true."
            )
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        logger.info("Starting Streamable HTTP MCP server at http://%s:%s/mcp", args.host, args.port)
    else:
        logger.info("Starting stdio MCP server")

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
