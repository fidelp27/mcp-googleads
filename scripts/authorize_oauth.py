"""Run the interactive Google OAuth flow and persist the refresh token."""

from google_ads_server import GOOGLE_ADS_AUTH_TYPE, get_credentials


def main() -> None:
    if GOOGLE_ADS_AUTH_TYPE.lower() != "oauth":
        raise SystemExit("GOOGLE_ADS_AUTH_TYPE must be oauth for this command")
    credentials = get_credentials()
    if not credentials or not credentials.valid:
        raise SystemExit("OAuth authorization did not produce valid credentials")
    print("OAuth authorization completed and the token was saved.")


if __name__ == "__main__":
    main()
