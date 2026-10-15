"""AMS filament mapping suggestions for reprints."""


def _hex_color(value):
    """Normalize '#RRGGBB', 'RRGGBB' or 'RRGGBBAA' to lower-case 'rrggbb'; None otherwise."""
    raw = (value or "").strip().lstrip("#").lower()
    if len(raw) in (6, 8) and all(ch in "0123456789abcdef" for ch in raw):
        return raw[:6]
    return None


def _same_color(required, slot):
    """A sliced file names colours as hex; a slot carries a name and/or a hex."""
    want = (required or "").strip().lower()
    if not want:
        return False
    if want == (slot.get("color") or "").strip().lower():
        return True
    want_hex = _hex_color(want)
    return want_hex is not None and want_hex == _hex_color(slot.get("color_hex"))


def suggest_mapping(required, available):
    """Match each source filament to a distinct tray: exact material+colour first,
    then material only. A reprint mapping may not reuse a tray."""
    suggested = []
    used = set()
    for req in required:
        material = (req["material"] or "").lower()
        candidates = [a for a in available if a["slot"] not in used and material and (a["material"] or "").lower() == material]
        exact = next((a for a in candidates if _same_color(req["color"], a)), None)
        best = exact or (candidates[0] if candidates else None)
        if best is not None:
            used.add(best["slot"])
            suggested.append({
                "source_slot": req["slot"],
                "target_slot": best["slot"],
                "confidence": "exact" if exact else "material_only",
            })
    return suggested
