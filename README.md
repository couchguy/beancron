# beancron

A crontab that lives in your Beancount ledger.

Schedules are `custom` entries in a `.beancount` file, so you can change *when* things run
(imports, price fetches, rules, reminders) from Fava's editor. *What* runs is defined in a
config file on the server, so the ledger never contains commands.

```beancount
2027-01-01 custom "beancron-option" "timezone" "America/Chicago"

2027-01-01 custom "schedule" "fetch"
  job: "fetch-bank"
  cron: "0 6 * * *"
  days: 7

2027-01-01 custom "schedule" "import"
  job: "import-bank"
  ledger: "main.beancount"
  after: "fetch"

2027-01-01 custom "schedule" "prices"
  job: "prices"
  cron: "30 17 * * 1-5"
```

## Install

```bash
git clone <repo> ~/projects/beancron
pip install -e ~/projects/beancron   # into the same environment as Beancount
```

## Config (on the server)

`~/.config/beancron/config.toml` (or set `BEANCRON_CONFIG`):

```toml
# The Beancount file holding your schedule entries. It can be your main ledger,
# or a separate file your ledger includes. Relative paths elsewhere start from its folder.
schedule = "~/ledger/schedule.beancount"

# The job registry: job name -> command. Any language.
[jobs.fetch-bank]
command = ["~/bin/fetch-bank.sh"]

[jobs.import-bank]
command = ["~/ledger/venv/bin/python", "~/ledger/import.py"]

[jobs.backup]
command = ["~/scripts/backup.sh"]
```

Python packages can also register jobs themselves, with an entry point in the
`beancron.jobs` group:

```toml
[project.entry-points."beancron.jobs"]
rules = "fava_rules.jobs:run"
```

## Schedule entries

```beancount
DATE custom "schedule" "UNIQUE-NAME"
  job: "JOB-NAME"
  ...
```

| Field | Meaning |
| --- | --- |
| date | The entry is active from this date |
| name (the quoted value) | Unique name; `after:` refers to it |
| `job` | Job to run, from the registry or an installed package |
| `cron` | Standard 5-field cron: minute hour day-of-month month day-of-week |
| `after` | Run when the named entry finishes successfully |
| `missed` | `skip` (default) or `run`: what to do if the machine was off at the cron time |
| `timeout` | Minutes before the job is stopped (default 30) |
| `enabled` | `FALSE` to turn an entry off without deleting it |
| anything else | Passed to the job as a parameter |

The schedule file can `include` other files (e.g. one per ledger: `include "*/schedule.beancount"`).
Each entry's `ledger:` path is relative to its own file, so `books/home/schedule.beancount` can say `ledger: "main.beancount"`.

An entry needs `cron`, `after`, or both.

## Options

```beancount
2027-01-01 custom "beancron-option" "timezone" "America/Chicago"
2027-01-01 custom "beancron-option" "log-dir" "logs"
```

- `timezone`: cron times are in this zone (default: the system's).
- `log-dir`: relative to the schedule file's folder (default `logs`).

## What a job receives

Jobs run in the schedule file's folder, with these environment variables:

| Variable | Value |
| --- | --- |
| `JOB_<KEY>` | Each extra field, e.g. `days: 7` becomes `JOB_DAYS=7` |
| `LEDGER` | If the entry has `ledger:`, the full path to that file. A relative path starts from the folder of the file the entry is in. |
| `BEANCRON_ENTRY` | The schedule entry's name |
| `BEANCRON_DIR` | The schedule file's folder |

Exit code 0 means success, and success is what triggers `after:` entries.
Python entry-point jobs are a function with no arguments that reads the same
environment and raises (or returns a non-zero number) to fail.

## Commands

```
beancron tick                 run whatever is due (the timer calls this every minute)
beancron run NAME             run one entry now
beancron run NAME --chain     ...and everything that runs after it
beancron list                 schedules, next run, last result
beancron check                problems in the schedule and registry
beancron jobs                 job names available
```

A manual `run --chain` ignores entry start dates, so you can test before they begin.

## Running it every minute

Copy the units in `systemd/`, set the user and paths, then:

```bash
sudo cp systemd/beancron.service systemd/beancron.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now beancron.timer
```

## Logs

In the log folder:

- `beancron.log`: one line per run: time, entry, result, duration
- `<entry>.log`: the output of that entry's last run
- `state.json`: last tick and last result per entry

Only one beancron runs jobs at a time. A tick that finds another still running exits,
and the next tick catches up. Cron times that passed while the machine was off are
skipped unless the entry says `missed: "run"`.

## License

MIT
