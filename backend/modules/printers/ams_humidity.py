"""Shared interpretation of stored Bambu humidity levels."""


def humidity_scale(humidity):
    """Return stored level, dry-to-wet index (1–5), and grade (A–E)."""
    level = int(humidity) if isinstance(humidity, (int, float)) and 1 <= humidity <= 5 else None
    index = 6 - level if level is not None else None
    return level, index, "ABCDE"[index - 1] if index else None
