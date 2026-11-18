"""Shared interpretation of stored Bambu humidity levels."""


def humidity_scale(humidity, temperature):
    """Return valid level, dry-to-wet index (1–5), grade (A–E), and sensor temperature."""
    level = int(humidity) if isinstance(humidity, (int, float)) and 1 <= humidity <= 5 else None
    # Older AMS lite rows use level 5 at exactly 0 degrees for an absent sensor.
    if level == 5 and temperature == 0:
        return None, None, None, None
    index = 6 - level if level is not None else None
    return level, index, "ABCDE"[index - 1] if index else None, temperature


def reported_percent(value):
    """Return a whole percentage when the sample has a valid measurement."""
    return round(value) if isinstance(value, (int, float)) and 0 <= value <= 100 else None
