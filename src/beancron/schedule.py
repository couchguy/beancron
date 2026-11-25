"""Read schedule entries and options from a Beancount file.

    2027-01-01 custom "schedule" "import"
      job: "import-bank"
      ledger: "main.beancount"
      after: "fetch"

    2027-01-01 custom "beancron-option" "timezone" "America/Chicago"

The ledger holds only names, times, and parameters. Never commands.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .cron import Cron, CronError

SCHEDULE_TYPE = "schedule"
OPTION_TYPE = "beancron-option"
KNOWN_OPTIONS = {"timezone", "log-dir"}
MISSED_CHOICES = {"skip", "run"}
DEFAULT_TIMEOUT_MINUTES = 30.0

# Metadata keys that control the scheduler instead of being passed to the job.
RESERVED = {"job", "cron", "after", "missed", "timeout", "enabled", "filename", "lineno"}


@dataclass
class Entry:
    name: str
    job: str | None
    where: str                      # "file:line", for messages
    active_from: dt.date
    cron: Cron | None = None
    after: str | None = None
    missed: str = "skip"
    timeout_minutes: float = DEFAULT_TIMEOUT_MINUTES
    enabled: bool = True
    params: dict[str, str] = field(default_factory=dict)
    valid: bool = True

    def trigger(self) -> str:
        parts = []
        if self.cron:
            parts.append(f"cron {self.cron.expression}")
        if self.after:
            parts.append(f"after {self.after}")
        return ", ".join(parts) or "-"


@dataclass
class Schedule:
    entries: list[Entry] = field(default_factory=list)
    options: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def get(self, name: str) -> Entry | None:
        return next((e for e in self.entries if e.name == name), None)

    def followers(self, name: str) -> list[Entry]:
        return [e for e in self.entries if e.after == name]


def _text(value) -> str:
    """Render a metadata value the way a job should receive it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def _values(custom) -> list:
    """Plain values of a Custom directive (Beancount wraps each one)."""
    return [getattr(v, "value", v) for v in custom.values]


def from_directives(directives) -> Schedule:
    """Build a Schedule from loaded Beancount directives (Custom entries only)."""
    schedule = Schedule()
    for d in directives:
        kind = getattr(d, "type", None)
        if kind == OPTION_TYPE:
            _read_option(d, schedule)
        elif kind == SCHEDULE_TYPE:
            entry = _read_entry(d, schedule.problems)
            if entry is None:
                continue
            if schedule.get(entry.name):
                schedule.problems.append(
                    f"{entry.where}: duplicate schedule name '{entry.name}' (ignored)")
                continue
            schedule.entries.append(entry)
    return schedule


def _read_option(d, schedule: Schedule) -> None:
    where = f"{d.meta.get('filename', '?')}:{d.meta.get('lineno', '?')}"
    values = _values(d)
    if len(values) != 2 or not all(isinstance(v, str) for v in values):
        schedule.problems.append(f"{where}: beancron-option needs a name and a value")
        return
    name, value = values
    if name not in KNOWN_OPTIONS:
        schedule.problems.append(f"{where}: unknown beancron-option '{name}'")
        return
    schedule.options[name] = value


def _read_entry(d, problems: list[str]) -> Entry | None:
    meta = d.meta or {}
    where = f"{meta.get('filename', '?')}:{meta.get('lineno', '?')}"
    values = _values(d)
    if len(values) != 1 or not isinstance(values[0], str):
        problems.append(f"{where}: schedule needs exactly one name, e.g. "
                        'custom "schedule" "fetch"')
        return None

    entry = Entry(name=values[0], job=None, where=where, active_from=d.date)

    def bad(message: str) -> None:
        problems.append(f"{where}: '{entry.name}': {message}")
        entry.valid = False

    job = meta.get("job")
    if isinstance(job, str) and job:
        entry.job = job
    else:
        bad("missing job (e.g. job: \"fetch-bank\")")

    cron = meta.get("cron")
    if cron is not None:
        try:
            entry.cron = Cron(str(cron))
        except CronError as e:
            bad(f"bad cron: {e}")

    after = meta.get("after")
    if after is not None:
        entry.after = str(after)

    if cron is None and after is None:
        bad("needs a cron time or an after:")

    missed = str(meta.get("missed", "skip"))
    if missed in MISSED_CHOICES:
        entry.missed = missed
    else:
        bad(f"missed must be one of {sorted(MISSED_CHOICES)}, not '{missed}'")

    timeout = meta.get("timeout")
    if timeout is not None:
        try:
            entry.timeout_minutes = float(timeout)
            if entry.timeout_minutes <= 0:
                raise ValueError
        except (TypeError, ValueError):
            bad(f"timeout must be a positive number of minutes, not '{timeout}'")

    enabled = meta.get("enabled", True)
    if isinstance(enabled, bool):
        entry.enabled = enabled
    else:
        bad("enabled must be TRUE or FALSE")

    entry.params = {k: _text(v) for k, v in meta.items()
                    if k not in RESERVED and not k.startswith("__")}
    return entry


def load(path: Path) -> Schedule:
    """Load schedule entries from a Beancount file."""
    from beancount import loader  # imported here so tests can run without Beancount

    directives, errors, _options = loader.load_file(str(path))
    schedule = from_directives(directives)
    for error in errors:
        schedule.problems.insert(0, f"{path}: {getattr(error, 'message', error)}")
    return schedule


def after_loops(schedule: Schedule) -> list[str]:
    """Names of entries that end up waiting on themselves through after: links."""
    looped = []
    for entry in schedule.entries:
        seen = {entry.name}
        current = entry
        while current and current.after:
            if current.after in seen:
                looped.append(entry.name)
                break
            seen.add(current.after)
            current = schedule.get(current.after)
    return looped
