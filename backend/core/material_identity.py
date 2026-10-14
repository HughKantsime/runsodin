"""Exact supported material labels, preserving temperature-sensitive variants."""
# Explicit protocol/material labels, preserving variants instead of collapsing
# their different print requirements into a generic family.
_MATERIALS = frozenset({
    'PLA', 'PLA-S', 'PLA-CF', 'PLA-SILK', 'PLA-MATTE', 'PLA-AERO',
    'PETG', 'PETG-CF', 'ABS', 'ABS-GF', 'ASA', 'TPU', 'TPU-95A',
    'TPU-83A', 'PA', 'PA-CF', 'PA-GF', 'PA6', 'PA6-CF', 'PA6-GF',
    'PA12-CF', 'PC', 'PC-ABS', 'PC-CF', 'PVA', 'HIPS', 'SUPPORT',
    'SUPPORT-W', 'SUPPORT-G',
})


def canonical_reported_material(value):
    """Return an exact recognized label; never infer a family or log raw input."""
    if not isinstance(value, str) or len(value) > 32 or any(ord(c) < 32 for c in value):
        return None
    normalized = value.strip().upper().replace('_', '-')
    return normalized if normalized in _MATERIALS else None


