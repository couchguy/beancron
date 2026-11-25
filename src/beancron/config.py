"""Server-side configuration: where the schedule file is, and the job registry.

This file lives on the server (default ~/.config/beancron/config.toml), not in
the ledger, so commands can only be added by someone with shell access.

    schedule = "~/ledger/schedule.beancount"

    [jobs.fetch-bank]
    command = ["~/bin/fetch-bank.sh"]
"""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path

ENTRY_POINT_GROUP = "beancron.jobs"

# Runs a Python job registered through an entry point ("package.module:function").
# The function takes no arguments, reads its settings from the environment,
# and fails by raising or returning a non-zero number.
_CALL_ENTRY_POINT = (
    "import importlib, sys\n"
    "module, _, func = sys.argv[1].partition(':')\n"
    "result = getattr(importlib.import_module(module), func)()\n"
    "sys.exit(result if isinstance(result, int) else 0)\n"
)


def default_path() -> Path:
    env = os.environ.get("BEANCRON_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "beancron" / "config.toml"


class ConfigError(Exception):
    pass


@dataclass
class Config:
    path: Path
    schedule: Path
    jobs: dict[str, list[str]] = field(default_factory=dict)

    @property
    def base_dir(self) -> Path:
        """The folder holding the schedule file. Relative paths start here."""
        return self.schedule.parent

    def command_for(self, job: str) -> list[str] | None:
        """The command for a job name: registry first, then installed packages."""
        if job in self.jobs:
            return list(self.jobs[job])
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            if ep.name == job:
                return [sys.executable, "-c", _CALL_ENTRY_POINT, ep.value]
        return None

    def known_jobs(self) -> dict[str, str]:
        """Every job name available, with where it comes from."""
        found = {ep.name: f"package ({ep.value})"
                 for ep in entry_points(group=ENTRY_POINT_GROUP)}
        found.update({name: "registry" for name in self.jobs})
        return dict(sorted(found.items()))


def load(path: Path | None = None) -> Config:
    path = path or default_path()
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"config not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None

    schedule = data.get("schedule")
    if not isinstance(schedule, str):
        raise ConfigError(f"{path}: 'schedule' must be the path to schedule.beancount")

    jobs: dict[str, list[str]] = {}
    for name, spec in data.get("jobs", {}).items():
        command = spec.get("command") if isinstance(spec, dict) else None
        if (not isinstance(command, list) or not command
                or not all(isinstance(part, str) for part in command)):
            raise ConfigError(
                f"{path}: jobs.{name}.command must be a list of strings, "
                'e.g. ["/path/to/python", "/path/to/script.py"]')
        jobs[name] = [str(Path(p).expanduser()) if p.startswith("~") else p for p in command]

    return Config(path=path, schedule=Path(schedule).expanduser(), jobs=jobs)
