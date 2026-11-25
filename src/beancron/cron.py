"""Standard 5-field cron expressions: minute hour day-of-month month day-of-week.

Supports *, numbers, ranges (1-5), lists (1,15), steps (*/15, 8-18/2), and
month/day names (jan, mon). Day-of-week 0 and 7 are both Sunday. As in classic
cron, when both day-of-month and day-of-week are restricted, a day matches if
either one matches.

All times are naive local wall-clock datetimes.
"""
from __future__ import annotations

import datetime as dt

_MONTHS = {n: i for i, n in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DAYS = {n: i for i, n in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}

# (field name, lowest, highest, names)
_FIELDS = [
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    ("month", 1, 12, _MONTHS),
    ("day of week", 0, 7, _DAYS),
]


class CronError(ValueError):
    """A cron expression that can't be parsed."""


def _value(text: str, low: int, high: int, names: dict, field: str) -> int:
    key = text.lower()
    if key in names:
        return names[key]
    try:
        value = int(text)
    except ValueError:
        raise CronError(f"{field}: '{text}' is not a number") from None
    if not low <= value <= high:
        raise CronError(f"{field}: {value} is outside {low}-{high}")
    return value


def _parse_field(text: str, low: int, high: int, names: dict, field: str) -> frozenset:
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"{field}: empty item in '{text}'")
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            try:
                step = int(step_text)
            except ValueError:
                raise CronError(f"{field}: step '{step_text}' is not a number") from None
            if step < 1:
                raise CronError(f"{field}: step must be 1 or more")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            first, last = base.split("-", 1)
            start = _value(first, low, high, names, field)
            end = _value(last, low, high, names, field)
            if start > end:
                raise CronError(f"{field}: range '{base}' runs backwards")
        else:
            start = _value(base, low, high, names, field)
            end = high if slash else start
        values.update(range(start, end + 1, step))
    return frozenset(values)


class Cron:
    """A parsed cron expression."""

    def __init__(self, expression: str):
        parts = expression.split()
        if len(parts) != 5:
            raise CronError(
                f"'{expression}' has {len(parts)} fields; expected 5 "
                "(minute hour day-of-month month day-of-week)")
        parsed = [_parse_field(p, *spec[1:3], spec[3], spec[0])
                  for p, spec in zip(parts, _FIELDS)]
        self.expression = expression
        self.minutes = sorted(parsed[0])
        self.hours = sorted(parsed[1])
        self.days = parsed[2]
        self.months = parsed[3]
        self.weekdays = frozenset(d % 7 for d in parsed[4])  # 7 -> 0 (Sunday)
        self._any_day = parts[2].startswith("*")
        self._any_weekday = parts[4].startswith("*")

    def __repr__(self) -> str:
        return f"Cron({self.expression!r})"

    def day_matches(self, day: dt.date) -> bool:
        if day.month not in self.months:
            return False
        by_day = day.day in self.days
        by_weekday = (day.isoweekday() % 7) in self.weekdays
        if self._any_day and self._any_weekday:
            return True
        if self._any_day:
            return by_weekday
        if self._any_weekday:
            return by_day
        return by_day or by_weekday

    def matches(self, moment: dt.datetime) -> bool:
        return (self.day_matches(moment.date())
                and moment.hour in self.hours
                and moment.minute in self.minutes)

    def last_in(self, after: dt.datetime, until: dt.datetime) -> dt.datetime | None:
        """Latest fire time t with after < t <= until, or None."""
        day = until.date()
        while day >= after.date():
            if self.day_matches(day):
                for hour in reversed(self.hours):
                    for minute in reversed(self.minutes):
                        moment = dt.datetime.combine(day, dt.time(hour, minute))
                        if after < moment <= until:
                            return moment
            day -= dt.timedelta(days=1)
        return None

    def next_after(self, after: dt.datetime, limit_days: int = 1500) -> dt.datetime | None:
        """Earliest fire time strictly after `after`, or None within the limit."""
        day = after.date()
        for _ in range(limit_days):
            if self.day_matches(day):
                for hour in self.hours:
                    for minute in self.minutes:
                        moment = dt.datetime.combine(day, dt.time(hour, minute))
                        if moment > after:
                            return moment
            day += dt.timedelta(days=1)
        return None
