import re
import time as tm


def StringToTime(time_str: str) -> int:
    """
    Parse time string and return seconds.
    Supports formats: 1s, 1m, 1h, 1d, 1w, 1sec, 1min, 1hrs, etc.
    Also supports Discord timestamps like <t:1778847300:t>

    Args:
        time_str: Time string like "1h", "30m", "1d2h"

    Returns:
        Total seconds as integer

    Raises:
        ValueError: If format is invalid or unit is unknown
    """
    time_units = {
        "s": 1,
        "sec": 1,
        "second": 1,
        "seconds": 1,
        "m": 60,
        "min": 60,
        "minute": 60,
        "minutes": 60,
        "h": 3600,
        "hr": 3600,
        "hrs": 3600,
        "hour": 3600,
        "hours": 3600,
        "d": 86400,
        "day": 86400,
        "days": 86400,
        "w": 604800,
        "week": 604800,
        "weeks": 604800,
    }

    cleaned = time_str.strip()

    timestamp_match = re.match(r"^<t:(\d+)(?::[tTdDfFR])?>$", cleaned)
    if timestamp_match:
        timestamp = int(timestamp_match.group(1))
        return timestamp - int(tm.time())

    pattern = r"^(\d+(?:\.\d+)?)\s*([a-zA-Z]+)$"
    match = re.match(pattern, cleaned.lower())

    if not match:
        raise ValueError("Invalid time format. Use format like '1h', '30m', '1d'")

    number = float(match.group(1))
    unit = match.group(2)

    if unit not in time_units:
        raise ValueError(f"Unknown time unit: {unit}. Use s, m, h, d, or w")

    return int(number * time_units[unit])





def TimeToString(seconds: int) -> str:
    """
    Format seconds into a human-readable string.

    Args:
        seconds: Number of seconds

    Returns:
        Formatted string like "1d 2h 30m 15s"
    """
    if seconds <= 0:
        return "0s"

    parts = []

    weeks, seconds = divmod(seconds, 604800)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    if weeks:
        parts.append(f"{weeks}w")
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds:
        parts.append(f"{seconds}s")

    return " ".join(parts)
