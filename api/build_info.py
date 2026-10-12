"""Image identity baked into source before packaging, never read from runtime env."""

import argparse
import re
from pathlib import Path

try:
    from ._build_revision import REVISION as BUILD_REVISION
except ModuleNotFoundError:
    BUILD_REVISION = None  # ordinary source/PyPI installs have no image revision


def write_build_revision(revision: str, expected_version: str, root: Path) -> None:
    # This command runs in the Python 3.12 build image before installing dependencies.
    import tomllib

    if revision != "unknown" and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        raise ValueError("build revision must be a full Git commit ID")
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if expected_version != "unknown" and version != expected_version:
        raise ValueError("build version does not match source metadata")
    (root / "api" / "_build_revision.py").write_text(f"REVISION = {revision!r}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision")
    parser.add_argument("version")
    args = parser.parse_args()
    write_build_revision(args.revision, args.version, Path.cwd())
