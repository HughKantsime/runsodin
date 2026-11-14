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


def attribute_source_grams(slot_grams, *, mapping, mapped, slots):
    """Assign each file source's grams to a spool, or say why it can't be.

    ``mapped``: only the stored mapping says which slot fed a source (Bambu AMS, Kobra S1 ACE).
    Otherwise file source N is slot N. ``slots`` is {slot_number: (spool_id or None, pending)}.
    Returns (grams_by_spool, slot_of_spool, unattributed). Callers charge nothing when any
    source is unattributed: a print attempt can be claimed only once.
    """
    grams_by_spool, slot_of, unattributed = {}, {}, []
    for source_slot, grams in slot_grams.items():
        if grams == 0:
            continue
        target = source_slot
        if mapped:
            if (not isinstance(mapping, list) or len(mapping) < source_slot
                    or type(mapping[source_slot - 1]) is not int or not 0 <= mapping[source_slot - 1] < 16):
                unattributed.append(dict(source_slot=source_slot, grams=grams, reason='missing_material_mapping'))
                continue
            target = mapping[source_slot - 1] + 1
        spool_id, pending = slots.get(target, (None, False))
        if pending:
            unattributed.append(dict(slot_number=target, grams=grams, reason='spool_change_pending'))
        elif spool_id is not None and grams > 0:
            grams_by_spool[spool_id] = grams_by_spool.get(spool_id, 0.0) + grams
            slot_of.setdefault(spool_id, target)
        else:
            unattributed.append(dict(slot_number=target, grams=grams, reason='no_bound_spool'))
    return grams_by_spool, slot_of, unattributed
