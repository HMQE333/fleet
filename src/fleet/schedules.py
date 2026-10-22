"""When a saved automation runs next. Plain date arithmetic — no AI involved.

Two shapes, chosen to cover real use without a cron parser:
  {"type": "daily", "time": "09:00", "days": [1, 2, 3, 4, 5]}   ISO weekdays, 1 = Monday
  {"type": "hourly", "every": 3}                                  every N hours, on the hour
"""

import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class ScheduleError(ValueError):
    pass


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ScheduleError(f"Unknown time zone “{name}”") from e


def validate(spec: dict | None) -> dict | None:
    """Return a normalised schedule, or raise ScheduleError with a message for the operator."""
    if spec is None:
        return None
    kind = spec.get("type")
    if kind == "daily":
        t = str(spec.get("time", ""))
        if not TIME_RE.match(t):
            raise ScheduleError("Time must look like 09:00")
        days = sorted({int(d) for d in spec.get("days") or range(1, 8)})
        if not days or any(d < 1 or d > 7 for d in days):
            raise ScheduleError("Pick at least one weekday")
        return {"type": "daily", "time": t, "days": days}
    if kind == "hourly":
        every = int(spec.get("every", 0))
        if not 1 <= every <= 24:
            raise ScheduleError("Run every 1 to 24 hours")
        return {"type": "hourly", "every": every}
    raise ScheduleError("Schedule type must be daily or hourly")


def next_run(spec: dict | None, tz_name: str, after: datetime) -> datetime | None:
    """First run strictly after `after` (UTC-aware), or None when there's no schedule."""
    if spec is None:
        return None
    if spec["type"] == "hourly":
        base = after.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        return base + timedelta(hours=spec["every"])
    tz = zone(tz_name)
    hh, mm = (int(x) for x in spec["time"].split(":"))
    local_now = after.astimezone(tz)
    for offset in range(0, 8):
        day = local_now.date() + timedelta(days=offset)
        candidate = datetime.combine(day, time(hh, mm), tzinfo=tz)
        if day.isoweekday() in spec["days"] and candidate > local_now:
            return candidate.astimezone(UTC)
    raise AssertionError("a validated daily schedule always has a slot within 8 days")


def describe(spec: dict | None, tz_name: str) -> str:
    if spec is None:
        return "Manual only"
    if spec["type"] == "hourly":
        return "Every hour" if spec["every"] == 1 else f"Every {spec['every']} hours"
    days = spec["days"]
    if days == [1, 2, 3, 4, 5, 6, 7]:
        when = "Every day"
    elif days == [1, 2, 3, 4, 5]:
        when = "Weekdays"
    else:
        when = ", ".join(DAY_NAMES[d - 1] for d in days)
    return f"{when} at {spec['time']} ({tz_name})"
