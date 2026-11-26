"""Colour comparisons shared by AMS mapping and printer inventory."""
import math


def hex_color(value):
    """Normalize '#RRGGBB', 'RRGGBB' or 'RRGGBBAA' to lower-case 'rrggbb'; None otherwise."""
    if not isinstance(value, str):
        return None
    raw = value.strip().lstrip("#").lower()
    if len(raw) in (6, 8) and all(ch in "0123456789abcdef" for ch in raw):
        return raw[:6]
    return None


def same_color(required, slot):
    """A sliced file names colours as hex; a slot carries a name and/or a hex."""
    want = (required or "").strip().lower()
    if not want:
        return False
    if want == (slot.get("color") or "").strip().lower():
        return True
    want_hex = hex_color(want)
    return want_hex is not None and want_hex == hex_color(slot.get("color_hex"))


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip('#')
    if len(hex_color) >= 6:
        return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))
    return (128, 128, 128)


def color_distance(hex1: str, hex2: str) -> float:
    r1, g1, b1 = hex_to_rgb(hex1)
    r2, g2, b2 = hex_to_rgb(hex2)
    return math.sqrt((r2-r1)**2 + (g2-g1)**2 + (b2-b1)**2)
