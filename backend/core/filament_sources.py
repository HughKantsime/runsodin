"""File source identities for consumption, including historical metadata."""


def indexed_materials(materials):
    """Return zero-based source IDs; never fill gaps in an explicit ID list."""
    if (not isinstance(materials, list) or not 1 <= len(materials) <= 16
            or any(not isinstance(item, dict) for item in materials)):
        return None
    if all('slot' not in item for item in materials):
        return dict(enumerate(materials))
    ids = [item.get('slot') for item in materials]
    if any(type(index) is not int for index in ids) or len(set(ids)) != len(ids):
        return None
    # Historical accounting records used a complete zero-based sequence.
    if ids == list(range(len(materials))):
        return dict(zip(ids, materials))
    if any(not 1 <= index <= 16 for index in ids):
        return None
    return {index - 1: item for index, item in zip(ids, materials)}
