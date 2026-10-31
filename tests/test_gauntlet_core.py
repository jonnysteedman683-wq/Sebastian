"""The shared Gauntlet core stays pure, pinned and re-exported (B76)."""

from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

import pytest

import sebastian.gauntlet as g
import sebastian.gauntlet_core as core

CORE = Path(core.__file__)
# Bump CORE_VERSION and this pin together, and update every vendored copy in the same cycle.
PINNED = {"1.0.0": "5d0e9c298f5d9a00c8fcd24d4724c07132d35fe13d5d5c56ee95d571c8e8415a"}


def test_core_imports_only_the_standard_library() -> None:
    """Vendoring needs zero dependencies: no host-package or third-party imports."""
    tree = ast.parse(CORE.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports would tie the core to a host package"
            mods.add((node.module or "").split(".")[0])
    assert mods <= set(sys.stdlib_module_names) | {"__future__"}, mods


def test_core_hash_matches_its_version() -> None:
    """Any edit to the core without a version bump fails here, so drift is never silent."""
    digest = hashlib.sha256(CORE.read_bytes()).hexdigest()
    assert PINNED.get(core.CORE_VERSION) == digest, (core.CORE_VERSION, digest)


@pytest.mark.parametrize(
    "name",
    [
        "Stage",
        "Prediction",
        "Transition",
        "Verdict",
        "GauntletError",
        "next_stage",
        "parse_prediction",
        "parse_verdict",
        "TERMINAL",
        "MAX_PREDICTED_FILES",
    ],
)
def test_gauntlet_reexports_the_core_objects(name: str) -> None:
    """Existing ``from sebastian.gauntlet import ...`` keeps getting the same objects."""
    assert getattr(g, name) is getattr(core, name)


def test_stage_formats_as_its_value() -> None:
    """Transition reasons read ``verify failed``, never ``Stage.VERIFY failed``."""
    assert f"{core.Stage.VERIFY} failed" == "verify failed"
    assert str(core.Stage.HELP) == "help"


def test_policy_is_structural() -> None:
    """Hosts pass their own policy objects; the core reads only three fields."""

    class Mini:
        max_loops = 2
        replan_after = 5
        checks_required = False

    t = core.next_stage(core.Stage.VERIFY, False, 1, Mini())
    assert (t.stage, t.loops) == (core.Stage.HELP, 2)
    assert core.parse_prediction("<checks>\nfile: a.py\n</checks>", Mini()).files == ("a.py",)
