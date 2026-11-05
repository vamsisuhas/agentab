from datetime import datetime, timedelta

FIELDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
SEARCH_DAYS = 366 * 8  # long enough to find any Feb 29


def _parse_field(field, lo, hi):
    values = set()
    for part in field.split(","):
        rng, _, step_text = part.partition("/")
        step = 1
        if step_text or "/" in part:
            if not step_text.isdigit() or int(step_text) == 0:
                raise ValueError(f"bad step in {field!r}")
            step = int(step_text)
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, _, b = rng.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"bad range in {field!r}")
            start, end = int(a), int(b)
        elif rng.isdigit():
            start = end = int(rng)
            if step_text:
                end = hi
        else:
            raise ValueError(f"bad value in {field!r}")
        if start < lo or end > hi or start > end:
            raise ValueError(f"out of range in {field!r}")
        values.update(range(start, end + 1, step))
    return values


def next_run(expression: str, after: datetime) -> datetime:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron expression must have 5 fields")
    minutes, hours, days, months, weekdays = (
        _parse_field(f, lo, hi) for f, (lo, hi) in zip(fields, FIELDS))
    weekdays = {0 if d == 7 else d for d in weekdays}
    both_restricted = fields[2] != "*" and fields[4] != "*"

    def day_matches(d):
        dom = d.day in days
        dow = (d.weekday() + 1) % 7 in weekdays
        if d.month not in months:
            return False
        return (dom or dow) if both_restricted else (dom and dow)

    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = start.replace(hour=0, minute=0)
    for _ in range(SEARCH_DAYS):
        if day_matches(day):
            for h in sorted(hours):
                for m in sorted(minutes):
                    candidate = day.replace(hour=h, minute=m)
                    if candidate >= start:
                        return candidate
        day += timedelta(days=1)
    raise ValueError(f"cron expression never matches: {expression!r}")
