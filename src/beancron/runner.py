"""Decide what's due and run it."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Config
from .schedule import Entry, Schedule, after_loops

MAX_LOOKBACK = dt.timedelta(days=31)
STATE_FILE = "state.json"
SUMMARY_FILE = "beancron.log"
LOCK_FILE = "beancron.lock"


def _parse_time(text: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def _env_name(key: str) -> str:
    return "JOB_" + re.sub(r"[^A-Za-z0-9]", "_", key).upper()


class Scheduler:
    def __init__(self, config: Config, schedule: Schedule):
        self.config = config
        self.schedule = schedule
        self.problems: list[str] = []
        self.tz: ZoneInfo | None = None
        tz_name = schedule.options.get("timezone")
        if tz_name:
            try:
                self.tz = ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, ValueError):
                self.problems.append(f"unknown timezone '{tz_name}'; using the system time zone")
        self.log_dir = config.base_dir / schedule.options.get("log-dir", "logs")
        self.state: dict = {}

    # --- time ---------------------------------------------------------------

    def now(self) -> dt.datetime:
        """Current local wall-clock time, to the minute."""
        moment = dt.datetime.now(self.tz) if self.tz else dt.datetime.now()
        return moment.replace(tzinfo=None, second=0, microsecond=0)

    def boot_time(self) -> dt.datetime | None:
        """When this machine last started, in local wall-clock time (Linux)."""
        try:
            for line in Path("/proc/stat").read_text().splitlines():
                if line.startswith("btime "):
                    stamp = int(line.split()[1])
                    moment = (dt.datetime.fromtimestamp(stamp, self.tz) if self.tz
                              else dt.datetime.fromtimestamp(stamp))
                    return moment.replace(tzinfo=None)
        except (OSError, ValueError):
            pass
        return None

    # --- files --------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self, wait: bool):
        """Only one beancron runs jobs at a time. Yields False if busy and not waiting."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with open(self.log_dir / LOCK_FILE, "w") as handle:
            flags = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(handle, flags)
            except BlockingIOError:
                yield False
                return
            try:
                self.state = self._load_state()
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _load_state(self) -> dict:
        try:
            return json.loads((self.log_dir / STATE_FILE).read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        path = self.log_dir / STATE_FILE
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, indent=2, sort_keys=True))
        temp.replace(path)

    def _summary(self, when: dt.datetime, name: str, text: str) -> None:
        with open(self.log_dir / SUMMARY_FILE, "a") as log:
            log.write(f"{when:%Y-%m-%d %H:%M}  {name}  {text}\n")

    # --- deciding what's due ------------------------------------------------

    def _active(self, entry: Entry, today: dt.date | None) -> bool:
        """Valid, enabled, and started. today=None ignores the start date (manual runs)."""
        started = today is None or entry.active_from <= today
        return entry.valid and entry.enabled and started

    def due(self, now: dt.datetime, last: dt.datetime,
            boot: dt.datetime | None) -> tuple[list[Entry], list[tuple[Entry, dt.datetime]]]:
        """Entries whose cron time fell in (last, now], and ones missed while off.

        A fire time before the machine's boot time was missed because the machine
        was off. Those are skipped unless the entry says missed: "run".
        """
        start = max(last, now - MAX_LOOKBACK)
        due, missed = [], []
        for order, entry in enumerate(self.schedule.entries):
            if not entry.cron or not self._active(entry, now.date()):
                continue
            fire = entry.cron.last_in(start, now)
            if fire is None:
                continue
            if boot is not None and fire < boot and entry.missed == "skip":
                missed.append((entry, fire))
            else:
                due.append((fire, order, entry))
        due.sort(key=lambda item: (item[0], item[1]))
        return [entry for _fire, _order, entry in due], missed

    # --- running ------------------------------------------------------------

    def tick(self, now: dt.datetime | None = None,
             boot: dt.datetime | None = None) -> list[tuple[str, str]]:
        """Run everything due since the last tick. Called every minute by the timer."""
        with self._locked(wait=False) as got_lock:
            if not got_lock:
                return []  # a previous tick is still running jobs; the next tick catches up
            now = now or self.now()
            boot = boot if boot is not None else self.boot_time()
            last = _parse_time(self.state.get("last_tick"))
            self.state["last_tick"] = now.isoformat()
            self._save_state()
            if last is None or now <= last:
                return []  # first tick ever, or the clock went back (DST)

            due, missed = self.due(now, last, boot)
            for entry, fire in missed:
                self._summary(now, entry.name, f"skipped: missed {fire:%Y-%m-%d %H:%M} while off")
            results: list[tuple[str, str]] = []
            ran: set[str] = set()
            for entry in due:
                self._run_chain(entry, ran, results, now.date())
            return results

    def run_now(self, name: str, chain: bool = False) -> list[tuple[str, str]]:
        """Run one entry right away (and its after: followers if chain)."""
        entry = self.schedule.get(name)
        if entry is None:
            raise KeyError(f"no schedule named '{name}'")
        if not entry.valid:
            raise ValueError(f"'{name}' has problems; run `beancron check`")
        with self._locked(wait=True):
            results: list[tuple[str, str]] = []
            if chain:
                self._run_chain(entry, set(), results, None)
            else:
                results.append((entry.name, self._run(entry)))
            return results

    def _run_chain(self, entry: Entry, ran: set[str],
                   results: list[tuple[str, str]], today: dt.date | None) -> None:
        if entry.name in ran:
            return
        ran.add(entry.name)
        status = self._run(entry)
        results.append((entry.name, status))
        if status != "ok":
            return
        for follower in self.schedule.followers(entry.name):
            if self._active(follower, today):
                self._run_chain(follower, ran, results, today)

    def environment(self, entry: Entry) -> dict[str, str]:
        env = dict(os.environ)
        env["BEANCRON_ENTRY"] = entry.name
        env["BEANCRON_DIR"] = str(self.config.base_dir)
        for key, value in entry.params.items():
            env[_env_name(key)] = value
        if "ledger" in entry.params:
            env["LEDGER"] = str(self.ledger_path(entry.params["ledger"]))
        return env

    def ledger_path(self, value: str) -> Path:
        """A ledger path from an entry: absolute, ~/..., or relative to the schedule's folder."""
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.config.base_dir / path

    def _run(self, entry: Entry) -> str:
        """Run one entry's job. Returns "ok" or a short failure reason."""
        started = self.now()
        clock = time.monotonic()
        command = self.config.command_for(entry.job or "")
        log_path = self.log_dir / f"{entry.name}.log"

        with open(log_path, "w") as log:
            log.write(f"# {entry.name} (job {entry.job}) started {started:%Y-%m-%d %H:%M}\n")
            log.flush()
            if command is None:
                status = f"failed: unknown job '{entry.job}'"
            elif "ledger" in entry.params and not self.ledger_path(entry.params["ledger"]).exists():
                status = f"failed: no ledger at {self.ledger_path(entry.params['ledger'])}"
            else:
                try:
                    result = subprocess.run(
                        command, cwd=self.config.base_dir, env=self.environment(entry),
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        timeout=entry.timeout_minutes * 60)
                    status = "ok" if result.returncode == 0 else f"failed: exit {result.returncode}"
                except subprocess.TimeoutExpired:
                    status = f"failed: timed out after {entry.timeout_minutes:g} min"
                except OSError as e:
                    status = f"failed: could not start ({e})"
            seconds = time.monotonic() - clock
            log.write(f"# {status} after {seconds:.0f}s\n")

        self.state.setdefault("entries", {})[entry.name] = {
            "last_run": started.isoformat(), "status": status, "seconds": round(seconds, 1)}
        self._save_state()
        self._summary(started, entry.name, f"{status} ({seconds:.0f}s)")
        return status

    # --- reporting ----------------------------------------------------------

    def rows(self) -> list[dict[str, str]]:
        """One row per entry for `beancron list`."""
        state = self._load_state().get("entries", {})
        now = self.now()
        rows = []
        for entry in self.schedule.entries:
            if not entry.valid:
                upcoming = "has problems"
            elif not entry.enabled:
                upcoming = "disabled"
            elif entry.cron:
                start = max(now, dt.datetime.combine(entry.active_from, dt.time()) - dt.timedelta(minutes=1))
                nxt = entry.cron.next_after(start)
                upcoming = f"{nxt:%a %Y-%m-%d %H:%M}" if nxt else "never"
            else:
                upcoming = "-"
            last = state.get(entry.name, {})
            rows.append({
                "name": entry.name,
                "job": entry.job or "?",
                "trigger": entry.trigger(),
                "next": upcoming,
                "last": (f"{last['last_run'].replace('T', ' ')[:16]}  {last['status']}"
                         if last else "never run"),
            })
        return rows

    def check(self) -> list[str]:
        """Everything wrong with the schedule and registry, as messages."""
        problems = list(self.schedule.problems) + list(self.problems)
        names = {e.name for e in self.schedule.entries}
        for entry in self.schedule.entries:
            if entry.job:
                command = self.config.command_for(entry.job)
                if command is None:
                    problems.append(f"{entry.where}: '{entry.name}': unknown job '{entry.job}' "
                                    f"(add it to {self.config.path} or install its package)")
                elif command[0].startswith("/") and not Path(command[0]).exists():
                    problems.append(f"job '{entry.job}': {command[0]} does not exist")
            if entry.after and entry.after not in names:
                problems.append(f"{entry.where}: '{entry.name}': after '{entry.after}', "
                                "but no schedule has that name")
            ledger = entry.params.get("ledger")
            if ledger and not self.ledger_path(ledger).exists():
                problems.append(f"{entry.where}: '{entry.name}': no ledger at "
                                f"{self.ledger_path(ledger)}")
        for name in after_loops(self.schedule):
            problems.append(f"'{name}': after: links loop back to itself")
        return problems
