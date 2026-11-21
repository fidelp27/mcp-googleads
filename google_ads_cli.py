"""Lightweight CLI dispatcher for `google-ads-mcp-server` and `google-ads-mcp`.

Routes `connect`, `register`, `doctor`, `serve`, and `alerts` BEFORE importing
`google_ads_server` or `agency`, so managed profiles populate `os.environ`
(`GOOGLE_ADS_MANAGED_PROFILE`, `GOOGLE_ADS_CONFIG_DIR`, `GOOGLE_ADS_DATA_DIR`,
`READ_ONLY_MODE`, `ALLOWED_CUSTOMER_IDS`, presets/modules) prior to server
initialization.
"""

from __future__ import annotations

import sys

_CONNECT_SUBCOMMANDS = frozenset({"connect", "register", "doctor", "serve"})


def _is_connect_argv(argv: list[str]) -> bool:
    """Return True when CLI args target a `google_ads_connect` subcommand."""
    idx = 0
    while idx < len(argv):
        token = argv[idx]
        if token == "--profile":
            idx += 2
            continue
        if token.startswith("--profile="):
            idx += 1
            continue
        return token in _CONNECT_SUBCOMMANDS
    return False


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)

    if args == ["remote"]:
        from agency.remote_gateway import main as remote_main

        remote_main()
        return

    if args and args[0] == "alerts":
        from agency.cli_alerts import main as alerts_main

        alerts_main(args[1:])
        return

    if _is_connect_argv(args):
        import google_ads_connect

        google_ads_connect.main(args)
        return

    import google_ads_server

    google_ads_server.main(args)


if __name__ == "__main__":
    main()
