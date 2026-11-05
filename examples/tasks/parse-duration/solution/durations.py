import re

UNITS = [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]
_PATTERN = re.compile(r"^(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?$")


def parse_duration(text):
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    cleaned = text.strip().lower()
    match = _PATTERN.match(cleaned)
    if not cleaned or not match or all(g is None for g in match.groups()):
        raise ValueError(f"invalid duration: {text!r}")
    return sum(int(g) * size for g, (_, size) in zip(match.groups(), UNITS) if g)


def format_duration(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise TypeError("seconds must be an int")
    if seconds < 0:
        raise ValueError("seconds must not be negative")
    if seconds == 0:
        return "0s"
    parts = []
    for unit, size in UNITS:
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count}{unit}")
    return " ".join(parts)
