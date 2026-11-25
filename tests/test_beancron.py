"""Tests that run without Beancount installed: directives are faked."""
import datetime as dt
import sys
import tempfile
import unittest
from collections import namedtuple
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from beancron.config import Config  # noqa: E402
from beancron.cron import Cron, CronError  # noqa: E402
from beancron.runner import Scheduler  # noqa: E402
from beancron.schedule import after_loops, from_directives  # noqa: E402

Custom = namedtuple("Custom", "meta date type values")
Value = namedtuple("Value", "value dtype")


def custom(kind, *values, line=1, date=dt.date(2027, 1, 1), **meta):
    meta = {"filename": "schedule.beancount", "lineno": line, **meta}
    return Custom(meta, date, kind, [Value(v, type(v)) for v in values])


def T(text):
    return dt.datetime.fromisoformat(text)


class CronTests(unittest.TestCase):
    def test_parsing(self):
        c = Cron("*/15 8-18 * * mon-fri")
        self.assertEqual(c.minutes, [0, 15, 30, 45])
        self.assertEqual(c.hours, list(range(8, 19)))
        self.assertEqual(c.weekdays, frozenset({1, 2, 3, 4, 5}))

    def test_sunday_is_0_and_7(self):
        self.assertTrue(Cron("0 9 * * 7").matches(T("2027-01-03 09:00")))  # a Sunday
        self.assertTrue(Cron("0 9 * * 0").matches(T("2027-01-03 09:00")))

    def test_day_or_weekday_when_both_set(self):
        c = Cron("0 0 1 * mon")  # the 1st, or any Monday
        self.assertTrue(c.matches(T("2027-01-01 00:00")))   # Friday the 1st
        self.assertTrue(c.matches(T("2027-01-04 00:00")))   # Monday the 4th
        self.assertFalse(c.matches(T("2027-01-05 00:00")))  # Tuesday the 5th

    def test_bad_expressions(self):
        for bad in ["* * * *", "60 * * * *", "* 24 * * *", "5-1 * * * *", "*/0 * * * *", "x * * * *"]:
            with self.assertRaises(CronError, msg=bad):
                Cron(bad)

    def test_last_in_and_next_after(self):
        c = Cron("30 17 * * 1-5")
        self.assertEqual(c.last_in(T("2027-01-04 17:29"), T("2027-01-04 17:30")), T("2027-01-04 17:30"))
        self.assertIsNone(c.last_in(T("2027-01-04 17:30"), T("2027-01-04 17:31")))
        # Friday evening -> next is Monday
        self.assertEqual(c.next_after(T("2027-01-08 18:00")), T("2027-01-11 17:30"))


class ScheduleParsingTests(unittest.TestCase):
    def test_entry_fields_and_params(self):
        s = from_directives([custom("schedule", "household-import", job="simplefin-import",
                                    after="fetch", ledger="personal/main.beancount", days=Decimal("7"),
                                    timeout=Decimal("5"))])
        self.assertEqual(s.problems, [])
        e = s.entries[0]
        self.assertEqual((e.name, e.job, e.after, e.timeout_minutes), ("household-import", "simplefin-import", "fetch", 5.0))
        self.assertEqual(e.params, {"ledger": "personal/main.beancount", "days": "7"})

    def test_problems(self):
        s = from_directives([
            custom("schedule", "a", line=1, cron="0 6 * * *"),                     # no job
            custom("schedule", "b", line=2, job="x"),                                # no trigger
            custom("schedule", "c", line=3, job="x", cron="99 * * * *"),             # bad cron
            custom("schedule", "d", line=4, job="x", cron="0 6 * * *", missed="later"),
            custom("schedule", "d", line=5, job="x", cron="0 6 * * *"),              # duplicate
            custom("beancron-option", "colour", "blue", line=6),
        ])
        text = "\n".join(s.problems)
        for expected in ["missing job", "needs a cron time", "bad cron", "missed must be",
                         "duplicate schedule name", "unknown beancron-option"]:
            self.assertIn(expected, text)

    def test_options(self):
        s = from_directives([custom("beancron-option", "timezone", "America/Chicago")])
        self.assertEqual(s.options, {"timezone": "America/Chicago"})

    def test_after_loops(self):
        s = from_directives([custom("schedule", "a", job="x", after="b"),
                             custom("schedule", "b", job="x", after="a", line=2)])
        self.assertEqual(sorted(after_loops(s)), ["a", "b"])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.books = Path(self.tmp.name) / "books"
        (self.books / "personal").mkdir(parents=True)
        (self.books / "personal" / "main.beancount").write_text("")
        self.out = Path(self.tmp.name) / "out.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def scheduler(self, directives, jobs=None):
        sh = "/bin/sh"
        default_jobs = {
            # Appends "<entry> <JOB_DAYS> <LEDGER>" to out.txt
            "record": [sh, "-c", f'echo "$BEANCRON_ENTRY ${{JOB_DAYS:-}} ${{LEDGER:-}}" >> {self.out}'],
            "fail": [sh, "-c", "echo boom; exit 3"],
            "slow": [sh, "-c", "sleep 5"],
        }
        config = Config(path=Path("config.toml"), schedule=self.books / "schedule.beancount",
                        jobs=jobs or default_jobs)
        return Scheduler(config, from_directives(directives))

    def ran(self):
        return self.out.read_text().splitlines() if self.out.exists() else []

    def test_first_tick_runs_nothing(self):
        s = self.scheduler([custom("schedule", "fetch", job="record", cron="* * * * *")])
        self.assertEqual(s.tick(now=T("2027-01-04 06:00"), boot=T("2027-01-01 00:00")), [])
        self.assertEqual(self.ran(), [])

    def test_runs_when_due_with_params(self):
        s = self.scheduler([custom("schedule", "fetch", job="record", cron="0 6 * * *",
                                   days=Decimal("7"), ledger="personal/main.beancount")])
        s.tick(now=T("2027-01-04 05:59"), boot=T("2027-01-01 00:00"))
        results = s.tick(now=T("2027-01-04 06:00"), boot=T("2027-01-01 00:00"))
        self.assertEqual(results, [("fetch", "ok")])
        self.assertEqual(self.ran(), [f"fetch 7 {self.books / 'personal' / 'main.beancount'}"])
        # Not again the next minute
        self.assertEqual(s.tick(now=T("2027-01-04 06:01"), boot=T("2027-01-01 00:00")), [])

    def test_after_chain_and_stop_on_failure(self):
        s = self.scheduler([
            custom("schedule", "fetch", job="record", cron="0 6 * * *", line=1),
            custom("schedule", "import", job="record", after="fetch", line=2),
            custom("schedule", "rules", job="record", after="import", line=3),
            custom("schedule", "broken", job="fail", cron="0 6 * * *", line=4),
            custom("schedule", "never", job="record", after="broken", line=5),
        ])
        s.tick(now=T("2027-01-04 05:59"), boot=T("2027-01-01 00:00"))
        results = dict(s.tick(now=T("2027-01-04 06:00"), boot=T("2027-01-01 00:00")))
        self.assertEqual([line.split()[0] for line in self.ran()], ["fetch", "import", "rules"])
        self.assertEqual(results["broken"], "failed: exit 3")
        self.assertNotIn("never", results)
        self.assertIn("boom", (s.log_dir / "broken.log").read_text())

    def test_missed_while_off_skip_and_run(self):
        s = self.scheduler([
            custom("schedule", "skipper", job="record", cron="0 6 * * *", line=1),
            custom("schedule", "catcher", job="record", cron="0 6 * * *", missed="run", line=2),
        ])
        s.tick(now=T("2027-01-04 05:00"), boot=T("2027-01-01 00:00"))
        # Machine was off 05:00-07:30; first tick after boot at 07:31
        results = s.tick(now=T("2027-01-04 07:31"), boot=T("2027-01-04 07:30"))
        self.assertEqual(results, [("catcher", "ok")])
        self.assertIn("skipped: missed 2027-01-04 06:00", (s.log_dir / "beancron.log").read_text())

    def test_late_but_not_off_still_runs(self):
        # Ticks were delayed (e.g. a long job held the lock) but the machine stayed on.
        s = self.scheduler([custom("schedule", "fetch", job="record", cron="0 6 * * *")])
        s.tick(now=T("2027-01-04 05:50"), boot=T("2027-01-01 00:00"))
        self.assertEqual(s.tick(now=T("2027-01-04 06:20"), boot=T("2027-01-01 00:00")), [("fetch", "ok")])

    def test_disabled_and_future_entries_do_not_run(self):
        s = self.scheduler([
            custom("schedule", "off", job="record", cron="0 6 * * *", enabled=False, line=1),
            custom("schedule", "later", job="record", cron="0 6 * * *", date=dt.date(2027, 2, 1), line=2),
        ])
        s.tick(now=T("2027-01-04 05:59"), boot=T("2027-01-01 00:00"))
        self.assertEqual(s.tick(now=T("2027-01-04 06:00"), boot=T("2027-01-01 00:00")), [])

    def test_timeout(self):
        s = self.scheduler([custom("schedule", "slow", job="slow", cron="0 6 * * *",
                                   timeout=Decimal("0.01"))])  # 0.6 seconds
        self.assertEqual(s.run_now("slow"), [("slow", "failed: timed out after 0.01 min")])

    def test_run_now_with_chain(self):
        s = self.scheduler([
            custom("schedule", "fetch", job="record", cron="0 6 * * *", line=1),
            custom("schedule", "import", job="record", after="fetch", line=2),
        ])
        self.assertEqual(s.run_now("fetch"), [("fetch", "ok")])
        self.assertEqual(s.run_now("fetch", chain=True), [("fetch", "ok"), ("import", "ok")])

    def test_manual_chain_skips_disabled_follower(self):
        s = self.scheduler([
            custom("schedule", "fetch", job="record", cron="0 6 * * *", line=1),
            custom("schedule", "import", job="record", after="fetch", enabled=False, line=2),
        ])
        self.assertEqual(s.run_now("fetch", chain=True), [("fetch", "ok")])


    def test_unknown_job_and_missing_ledger(self):
        s = self.scheduler([
            custom("schedule", "a", job="nope", cron="0 6 * * *", line=1),
            custom("schedule", "b", job="record", cron="0 6 * * *", ledger="ghost/main.beancount", line=2),
            custom("schedule", "c", job="record", after="zzz", line=3),
        ])
        text = "\n".join(s.check())
        self.assertIn("unknown job 'nope'", text)
        self.assertIn("no ledger at", text)
        self.assertIn("after 'zzz'", text)
        self.assertEqual(s.run_now("a"), [("a", "failed: unknown job 'nope'")])

    def test_ledger_paths(self):
        s = self.scheduler([
            custom("schedule", "a", job="record", cron="0 6 * * *", ledger="main.beancount", line=1),
            custom("schedule", "b", job="record", cron="0 6 * * *", ledger="/srv/main.beancount", line=2),
            custom("schedule", "c", job="record", cron="0 6 * * *", ledger="~/x.beancount", line=3),
            # An entry from an included file: relative to that file's folder
            custom("schedule", "d", job="record", cron="0 6 * * *", ledger="main.beancount", line=4,
                   filename=str(self.books / "personal" / "schedule.beancount")),
        ])
        paths = [s.ledger_path(e) for e in s.schedule.entries]
        self.assertEqual(paths, [self.books / "main.beancount", Path("/srv/main.beancount"),
                                 Path.home() / "x.beancount",
                                 self.books / "personal" / "main.beancount"])

    def test_list_rows(self):
        s = self.scheduler([custom("schedule", "prices", job="record", cron="30 17 * * 1-5")])
        row = s.rows()[0]
        self.assertEqual(row["trigger"], "cron 30 17 * * 1-5")
        self.assertEqual(row["last"], "never run")


class CliTests(unittest.TestCase):
    """Smoke test the commands, with the Beancount loader swapped for fake directives."""

    def test_commands(self):
        import contextlib
        import io
        from unittest import mock

        from beancron import cli, schedule

        with tempfile.TemporaryDirectory() as tmp:
            books = Path(tmp) / "books"
            books.mkdir()
            (books / "schedule.beancount").write_text("")
            cfg = Path(tmp) / "config.toml"
            cfg.write_text(f'schedule = "{books}/schedule.beancount"\n'
                           '[jobs.hello]\ncommand = ["/bin/sh", "-c", "echo hi"]\n')
            fake = schedule.from_directives([custom("schedule", "greet", job="hello", cron="0 6 * * *")])
            with mock.patch.object(schedule, "load", return_value=fake):
                for argv, code, expect in [
                    (["list"], 0, "greet"),
                    (["check"], 0, "OK: 1 schedule"),
                    (["jobs"], 0, "hello  (registry)"),
                    (["run", "greet"], 0, "greet: ok"),
                    (["run", "nope"], 2, ""),
                ]:
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(cli.main(["--config", str(cfg), *argv]), code, argv)
                    self.assertIn(expect, out.getvalue(), argv)
                self.assertIn("hi", (books / "logs" / "greet.log").read_text())


if __name__ == "__main__":
    unittest.main()
