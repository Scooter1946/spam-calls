from __future__ import annotations

from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("pitchloop generated-tool conformance")
    group.addoption(
        "--generated-dir",
        action="store",
        default="generated_tools",
        help="directory containing the generated Fact B implementation and manifest",
    )


@pytest.fixture
def generated_dir(request: pytest.FixtureRequest) -> Path:
    path = Path(request.config.getoption("--generated-dir")).resolve()
    if not path.is_dir():
        pytest.fail(f"generated tool directory does not exist: {path}")
    return path
