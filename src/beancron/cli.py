"""beancron: a crontab that lives in your Beancount ledger.

    beancron tick            run whatever is due (the system timer calls this every minute)
    beancron run NAME        run one schedule entry now
    beancron run NAME --chain   ...and everything set to run after it
    beancron list            schedules, next run, last result
    beancron check           find problems in the schedule and job registry
    beancron jobs            job names available to schedule
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config as config_mod
from . import schedule as schedule_mod
from .runner import Scheduler


def _scheduler(args) -> Scheduler:
    config = config_mod.load(Path(args.config) if args.config else None)
    if not config.schedule.exists():
        raise config_mod.ConfigError(f"schedule file not found: {config.schedule}")
    return Scheduler(config, schedule_mod.load(config.schedule))


def _table(rows: list[dict[str, str]], columns: list[str]) -> str:
    widths = {c: max(len(c), *(len(r[c]) for r in rows)) for c in columns}
    lines = ["  ".join(c.upper().ljust(widths[c]) for c in columns)]
    lines += ["  ".join(r[c].ljust(widths[c]) for c in columns) for r in rows]
    return "\n".join(line.rstrip() for line in lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="beancron", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help=f"config file (default {config_mod.default_path()})")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("tick", help="run whatever is due")
    run = commands.add_parser("run", help="run one schedule entry now")
    run.add_argument("name")
    run.add_argument("--chain", action="store_true", help="also run its after: followers")
    commands.add_parser("list", help="show schedules")
    commands.add_parser("check", help="find problems")
    commands.add_parser("jobs", help="show available job names")
    args = parser.parse_args(argv)

    try:
        scheduler = _scheduler(args)
    except config_mod.ConfigError as e:
        print(f"beancron: {e}", file=sys.stderr)
        return 2

    if args.command == "tick":
        results = scheduler.tick()
        return 1 if any(status != "ok" for _name, status in results) else 0

    if args.command == "run":
        try:
            results = scheduler.run_now(args.name, chain=args.chain)
        except (KeyError, ValueError) as e:
            print(f"beancron: {e.args[0]}", file=sys.stderr)
            return 2
        for name, status in results:
            print(f"{name}: {status}  (log: {scheduler.log_dir / (name + '.log')})")
        return 1 if any(status != "ok" for _name, status in results) else 0

    if args.command == "list":
        rows = scheduler.rows()
        if not rows:
            print(f"No schedules in {scheduler.config.schedule}")
        else:
            print(_table(rows, ["name", "job", "trigger", "next", "last"]))
        problems = scheduler.check()
        if problems:
            print(f"\n{len(problems)} problem(s); run `beancron check` for details")
        return 0

    if args.command == "check":
        problems = scheduler.check()
        for problem in problems:
            print(problem)
        if not problems:
            print(f"OK: {len(scheduler.schedule.entries)} schedule(s), no problems")
        return 1 if problems else 0

    if args.command == "jobs":
        jobs = scheduler.config.known_jobs()
        if not jobs:
            print(f"No jobs. Add some to {scheduler.config.path}")
        for name, source in jobs.items():
            print(f"{name}  ({source})")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
