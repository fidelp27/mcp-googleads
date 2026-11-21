"""Customizable analysis profiles (Markdown templates + TOML parameters).

Profiles live in `CONFIG_DIR/profiles` (falling back to the repository's
built-in `config/profiles` directory). Each profile pairs:
- `<id>.toml`: reproducible numeric/filter parameters and alert thresholds.
- `<id>.md`: editable Markdown audit instructions and output structure.

Security boundary:
Profiles express analysis preferences only. Account access (`ALLOWED_CUSTOMER_IDS`),
`READ_ONLY_MODE`, budget/bid caps, and human approvals are always enforced by the
server and cannot be relaxed by any profile.
"""

from __future__ import annotations

from importlib import resources as importlib_resources
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from .api import format_json
from .config import CONFIG_DIR, PROJECT_ROOT, ensure_tool_allowed, load_json_config
from .governance import audited_tool


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _effective_config_dir() -> Path:
    env_dir = os.getenv("GOOGLE_ADS_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return CONFIG_DIR


def _builtin_package_profiles_dir() -> Path | None:
    try:
        ref = importlib_resources.files("agency").joinpath("builtin_profiles")
        candidate = Path(str(ref))
        if candidate.is_dir():
            return candidate
    except (ModuleNotFoundError, TypeError, OSError):
        pass
    local_pkg = Path(__file__).resolve().parent / "builtin_profiles"
    return local_pkg if local_pkg.is_dir() else None


def _profile_sources() -> list[tuple[Path, str]]:
    """Return `(directory, source_label)` pairs in precedence order.

    1. User `CONFIG_DIR/profiles` (editable local overrides).
    2. Checkout `PROJECT_ROOT/config/profiles` (when running inside repo).
    3. Packaged `agency/builtin_profiles` (bundled inside wheel for `uvx`/`pip install`).
    """
    sources: list[tuple[Path, str]] = []
    seen_resolved: set[Path] = set()

    def _add(path: Path | None, label: str) -> None:
        if path is None or not path.is_dir():
            return
        resolved = path.resolve()
        if resolved in seen_resolved:
            return
        seen_resolved.add(resolved)
        sources.append((path, label))

    user_dir = _effective_config_dir() / "profiles"
    checkout_dir = PROJECT_ROOT / "config" / "profiles"
    pkg_dir = _builtin_package_profiles_dir()

    if user_dir.is_dir() and (
        not checkout_dir.is_dir() or user_dir.resolve() != checkout_dir.resolve()
    ):
        _add(user_dir, "user")
    _add(checkout_dir, "builtin")
    _add(pkg_dir, "builtin")
    return sources


def _validate_profile_id(profile_id: str) -> str:
    clean = (profile_id or "").strip().lower()
    if not _PROFILE_ID_RE.fullmatch(clean):
        raise ValueError(
            "profile_id must contain only lowercase letters, digits, hyphens or underscores"
        )
    return clean


def _load_profile_from_dir(directory: Path, profile_id: str, source: str) -> dict[str, Any]:
    toml_path = directory / f"{profile_id}.toml"
    md_path = directory / f"{profile_id}.md"
    if not toml_path.is_file() and not md_path.is_file():
        raise FileNotFoundError(profile_id)

    meta: dict[str, Any] = {}
    if toml_path.is_file():
        meta = tomllib.loads(toml_path.read_text(encoding="utf-8"))

    instructions = md_path.read_text(encoding="utf-8").strip() if md_path.is_file() else ""
    alert_defaults = load_json_config("alerts.json", {}).get("defaults", {})
    raw_thresholds = meta.get("thresholds") or {}
    merged_thresholds = {**alert_defaults, **raw_thresholds}

    return {
        "id": str(meta.get("id") or profile_id),
        "name": str(meta.get("name") or profile_id.replace("-", " ").title()),
        "description": str(meta.get("description") or ""),
        "version": str(meta.get("version") or "1.0"),
        "source": source,
        "recommended_modules": list(meta.get("recommended_modules") or ["core"]),
        "parameters": dict(meta.get("parameters") or {}),
        "thresholds": merged_thresholds,
        "has_markdown_template": bool(instructions),
        "instructions_markdown": instructions,
    }


def list_profiles() -> list[dict[str, Any]]:
    """Discover all available analysis profiles (user CONFIG_DIR overrides built-in)."""
    seen: dict[str, dict[str, Any]] = {}
    for directory, source in _profile_sources():
        ids = {
            path.stem.lower()
            for path in list(directory.glob("*.toml")) + list(directory.glob("*.md"))
            if _PROFILE_ID_RE.fullmatch(path.stem.lower())
        }
        for pid in sorted(ids):
            if pid in seen:
                continue
            seen[pid] = _load_profile_from_dir(directory, pid, source)
    return [seen[key] for key in sorted(seen)]


def load_profile(profile_id: str) -> dict[str, Any]:
    """Load a single profile by ID."""
    clean = _validate_profile_id(profile_id)
    for directory, source in _profile_sources():
        try:
            return _load_profile_from_dir(directory, clean, source)
        except FileNotFoundError:
            continue
    available = [item["id"] for item in list_profiles()]
    raise ValueError(
        f"Unknown analysis profile '{clean}'. Available profiles: {available}"
    )


def profile_thresholds(profile_id: str) -> dict[str, Any]:
    """Return effective alert/analysis thresholds for a profile."""
    return dict(load_profile(profile_id)["thresholds"])


def register_analysis_profile_tools(mcp) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def list_analysis_profiles() -> str:
        """List editable local analysis profiles (Markdown templates + TOML parameters)."""
        ensure_tool_allowed("list_analysis_profiles")
        profiles = [
            {
                "id": item["id"],
                "name": item["name"],
                "description": item["description"],
                "version": item["version"],
                "source": item["source"],
                "recommended_modules": item["recommended_modules"],
                "has_markdown_template": item["has_markdown_template"],
                "parameters": item["parameters"],
                "thresholds": item["thresholds"],
            }
            for item in list_profiles()
        ]
        return format_json(
            {
                "count": len(profiles),
                "profiles_dir": str(_effective_config_dir() / "profiles"),
                "profiles": profiles,
                "security_notice": (
                    "Analysis profiles customize instructions, parameters, and thresholds only. "
                    "Server-side account allowlists, READ_ONLY_MODE, caps, and approvals always apply."
                ),
            }
        )

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_analysis_profile(
        profile_id: str = Field(description="Profile ID (e.g. 'leads-audit' or 'ecommerce-audit')"),
        customer_id: Optional[str] = Field(
            default=None,
            description="Optional 10-digit customer ID to include in the reproducibility header",
        ),
    ) -> str:
        """Load an analysis profile's Markdown instructions, TOML parameters, and merged thresholds."""
        ensure_tool_allowed("get_analysis_profile")
        profile = load_profile(profile_id)
        return format_json(
            {
                **profile,
                "reproducibility_header": {
                    "profile_id": profile["id"],
                    "profile_version": profile["version"],
                    "customer_id": customer_id,
                    "parameters": profile["parameters"],
                    "thresholds": profile["thresholds"],
                },
                "security_notice": (
                    "Include the reproducibility_header in the final report. "
                    "This profile cannot override READ_ONLY_MODE, ALLOWED_CUSTOMER_IDS, or mutation approvals."
                ),
            }
        )

    @mcp.prompt()
    def analysis_profile_prompt(profile_id: str, customer_id: str = "") -> str:
        """Run a customizable Google Ads audit using a local Markdown + TOML analysis profile."""
        profile = load_profile(profile_id)
        header = (
            f"Use analysis profile `{profile['id']}` (v{profile['version']} — {profile['name']}).\n"
            f"Target customer_id: {customer_id or '<select from list_accounts>'}\n"
            f"Recommended modules: {', '.join(profile['recommended_modules'])}\n"
            f"Effective TOML parameters:\n```json\n{format_json(profile['parameters'])}\n```\n"
            f"Effective thresholds:\n```json\n{format_json(profile['thresholds'])}\n```\n\n"
            "Security reminder: Profiles only define analysis preferences; server guardrails "
            "(READ_ONLY_MODE, ALLOWED_CUSTOMER_IDS, dry-run approvals) always apply.\n\n"
        )
        return header + profile["instructions_markdown"]
