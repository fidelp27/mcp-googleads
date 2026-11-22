"""Launch the Claude Desktop extension over stdio."""

import os
from pathlib import Path


def main() -> None:
    from google_ads_connect import default_profile

    mode = os.environ.pop("GOOGLE_ADS_DESKTOP_MODE", "").strip().lower() or "demo"
    profile_override = os.environ.pop("GOOGLE_ADS_DESKTOP_PROFILE", "").strip()
    if mode not in {"demo", "account"}:
        raise SystemExit("Set Mode to demo or account in the extension settings.")

    # Claude inherits its environment. Keep this instance independent of any
    # other Google Ads integration or .env file on the machine.
    for name in tuple(os.environ):
        if name.startswith(("GOOGLE_ADS_", "MCP_REMOTE_", "DEMO_")):
            os.environ.pop(name)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["READ_ONLY_MODE"] = "true"

    if mode == "demo":
        from google_ads_server import main as run_server

        run_server(["--transport", "stdio", "--demo", "--preset", "core"])
        return

    profile = Path(profile_override).expanduser() if profile_override else default_profile()
    if not profile.is_file():
        raise SystemExit("Google Ads profile missing. Run the connect command or select demo mode.")

    from google_ads_connect import main as run_connect

    run_connect(["--profile", str(profile.resolve()), "serve", "--preset", "core"])


if __name__ == "__main__":
    main()
