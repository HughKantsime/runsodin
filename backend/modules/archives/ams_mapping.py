"""AMS filament mapping suggestions for reprints."""


from core.filament_color import same_color as _same_color


def suggest_mapping(required, available):
    """Match each source filament to a distinct tray: exact material+colour first,
    then material only. A reprint mapping may not reuse a tray."""
    suggested = []
    used = set()
    for req in required:
        material = (req["material"] or "").lower()
        candidates = [a for a in available if not a.get("do_not_feed") and a["slot"] not in used and material and (a["material"] or "").lower() == material]
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
