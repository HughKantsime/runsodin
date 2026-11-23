"""Bounded printable text from uploaded slice metadata."""
MAX_FACT_TEXT = 128


def fact_text(value):
    if type(value) is not str:
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()[:MAX_FACT_TEXT]
    return cleaned or None
