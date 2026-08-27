#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""V26/V27: the plugin's contract with Pipecat is public API at a tested floor.

B1 shipped both halves of the same mistake. The package imported the private
``pipecat.services.settings._NotGiven``, which 1.8.0 moved and made public as
``NotGiven`` — so every import of ``pipecat_slng`` died. And the declared floor
``pipecat-ai>=1.3.0`` was never exercised above 1.3.0, because ``uv.lock``
pinned 1.3.0 and CI installs from the lock. A range nothing tests is a range
that is wrong.
"""

import ast
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SRC = ROOT / "src" / "pipecat_slng"


def _pipecat_imports(tree: ast.AST) -> list[tuple[str, str]]:
    """Collect ``(module, name)`` pairs this module imports from Pipecat.

    Args:
            tree: Parsed AST of one source file.

    Returns:
            One pair per imported name, plus ``(module, "")`` for bare
            ``import pipecat.x`` statements.
    """
    pairs: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] == "pipecat":
                pairs += [(module, alias.name) for alias in node.names]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "pipecat":
                    pairs.append((alias.name, ""))
    return pairs


@pytest.mark.parametrize("path", sorted(SRC.glob("*.py")), ids=lambda p: p.name)
def test_v26_no_private_pipecat_api(path: Path):
    """No module reads a leading-underscore name out of Pipecat."""
    private = [
        f"{module}.{name}" if name else module
        for module, name in _pipecat_imports(ast.parse(path.read_text()))
        for part in module.split(".") + ([name] if name else [])
        if part.startswith("_")
    ]
    assert not private, (
        f"{path.name} depends on private Pipecat API, which moves without "
        f"notice (V26): {sorted(set(private))}"
    )


def test_v27_lock_pins_the_declared_floor():
    """The locked Pipecat is exactly the floor, so CI tests what we promise."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    floors = {
        dep.split(">=")[1]
        for group in (
            pyproject["project"]["dependencies"],
            *pyproject["project"]["optional-dependencies"].values(),
        )
        for dep in group
        if dep.split("[")[0].split(">=")[0] == "pipecat-ai"
    }
    assert len(floors) == 1, f"pipecat-ai floor declared inconsistently: {floors}"

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = next(p["version"] for p in lock["package"] if p["name"] == "pipecat-ai")
    assert locked == floors.pop(), (
        f"uv.lock pins pipecat-ai {locked} but the floor is {floors}; CI installs "
        "from the lock, so the untested half of the range is a false promise (V27)"
    )


@pytest.mark.parametrize(
    "settings_name, field",
    [
        ("SlngSTTSettings", "enable_vad"),
        ("SlngSTTSettings", "enable_partials"),
        ("SlngTTSSettings", "speed"),
    ],
)
def test_v26_sentinel_round_trips(settings_name: str, field: str):
    """An omitted field is NOT_GIVEN; an explicitly set one is given.

    Imports live in the body on purpose: a private-API break makes importing
    ``pipecat_slng`` raise, and a module-level import here would turn the V26
    check above into a collection error that never names the cause.
    """
    import pipecat_slng
    from pipecat.services.settings import is_given

    settings_cls = getattr(pipecat_slng, settings_name)
    assert not is_given(getattr(settings_cls(), field))
    assert is_given(getattr(settings_cls(**{field: 1.0}), field))
