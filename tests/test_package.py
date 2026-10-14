import tomllib
from importlib.metadata import version
from pathlib import Path

import quant_research


def test_package_version_matches_installed_metadata() -> None:
    assert quant_research.__version__ == version("quant-research")


def test_installed_version_matches_project_metadata() -> None:
    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with project_file.open("rb") as project_stream:
        project = tomllib.load(project_stream)

    assert version("quant-research") == project["project"]["version"]
