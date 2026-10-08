"""evaldiff API — FastAPI server for datasets, runs, and diffs."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:  # stay in sync with the installed distribution
    __version__ = _pkg_version("evaldiff")
except PackageNotFoundError:  # dev checkout without install
    __version__ = "0.0.11"
