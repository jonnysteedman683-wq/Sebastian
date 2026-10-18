"""Sebastian: an autonomous, sandboxed, gated tool-using agent."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sebastian")
except PackageNotFoundError:  # pragma: no cover - running from a source checkout
    __version__ = "0.0.0+unknown"

from sebastian.agent import Sebastian
from sebastian.budget import Budget
from sebastian.types import RunResult, StopReason

__all__ = ["Budget", "RunResult", "Sebastian", "StopReason", "__version__"]
