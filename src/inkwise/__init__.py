"""Inkwise reference core. No external inference is enabled by default."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib


def _source_version():
    # Local distributions reuse an older dependency environment; prefer the
    # selected, verified source metadata over that environment's package record.
    project = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if project.is_file():
        with project.open("rb") as stream:
            metadata = tomllib.load(stream).get("project", {})
        if metadata.get("name") == "inkwise-k12":
            return metadata["version"]
    try:
        return version("inkwise-k12")
    except PackageNotFoundError:
        return "unknown"


__version__ = _source_version()
