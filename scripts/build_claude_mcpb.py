"""Build a Claude Desktop bundle from an allowlist of distributable files."""

import argparse
import json
from pathlib import Path
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    packaging = root / "packaging" / "claude-desktop"
    manifest = json.loads((packaging / "manifest.json").read_text(encoding="utf-8"))
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    if manifest["version"] != project["project"]["version"]:
        parser.error("The manifest version must match pyproject.toml.")

    files = {
        "manifest.json": packaging / "manifest.json",
        "desktop.py": packaging / "desktop.py",
        "README.md": packaging / "README.md",
    }
    for name in ("pyproject.toml", "LICENSE", "google_ads_cli.py", "google_ads_connect.py", "google_ads_server.py"):
        files[name] = root / name
    for pattern in ("agency/*.py", "agency/builtin_profiles/*.md", "agency/builtin_profiles/*.toml"):
        for source in sorted(root.glob(pattern)):
            files[source.relative_to(root).as_posix()] = source

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.output, "x", compression=ZIP_DEFLATED) as bundle:
        for name, source in files.items():
            bundle.writestr(name, source.read_bytes())
    print(args.output.resolve())


if __name__ == "__main__":
    main()
