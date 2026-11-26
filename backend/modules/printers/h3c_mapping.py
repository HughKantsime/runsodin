"""Plan onboard H3C positions from verified slice requirements and tray inventory."""
from functools import lru_cache
from math import isclose

from sqlalchemy import text

from core.h3c_plate import RackMessage
from core.filament_color import color_distance, hex_color, same_color
from core.material_identity import canonical_reported_material
from modules.printers.nozzle_capability import position_refusal, rack_tools


def onboard_inventory(db, printer_id):
    return [dict(row) for row in db.execute(text(
        'SELECT slot_number AS slot, color, color_hex, filament_type AS material, do_not_feed '
        'FROM filament_slots WHERE printer_id=:id AND slot_number BETWEEN 1 AND 9 ORDER BY slot_number'
    ), {'id': printer_id}).mappings()]


def material_refusal(source, position, loaded):
    if loaded == 'empty':
        return RackMessage('tray_material_unreported', f'Position {position} is empty.')
    actual = canonical_reported_material(loaded)
    if actual is None:
        return RackMessage('tray_material_unreported', f'The material at position {position} is unreported.')
    if actual != source['material']:
        return RackMessage('material_mismatch', f"Filament {source['source_index'] + 1} needs {source['material']}. Position {position} holds {actual}.")
    return None


def color_warning(source, position, loaded_hex):
    color = hex_color(loaded_hex)
    if source.get('color') and color and not same_color(source['color'], {'color_hex': color}):
        return dict(filament=source['source_index'] + 1, position=position,
                    file_color=source['color'], loaded_color='#' + color.upper())


def start_color_warning(source, position, live, inventory):
    loaded = hex_color(live)
    previous = hex_color(inventory)
    warning = color_warning(source, position, loaded or previous)
    if warning:
        if not loaded:
            warning['color_source'] = 'last_reported'
        comparison = f"file {warning['file_color']}, loaded {warning['loaded_color']}"
    elif loaded and previous and loaded != previous:
        warning = dict(filament=source['source_index'] + 1, position=position, file_color=source.get('color'),
                       previous_color='#' + previous.upper(), loaded_color='#' + loaded.upper())
        comparison = f"previously {warning['previous_color']}, loaded {warning['loaded_color']}"
    else:
        return None
    warning['message'] = f"Filament {warning['filament']} colour differs at position {position} ({comparison})."
    return warning


def rack_candidates(sources, available, installed):
    tools = rack_tools(installed)
    return [[tray for tray in available if 1 <= tray['slot'] <= 9 and not tray.get('do_not_feed')
             and not material_refusal(source, tray['slot'], tray.get('material'))
             and not position_refusal(source, tray['slot'], tools)] for source in sources]


def derive_mapping(sources, candidates, *, selection_notes=None):
    """Find a unique best complete assignment. Nine trays bound the subset search."""
    def choose(index):
        return ValueError(RackMessage('ams_mapping_required', f"Choose a mapping for filament {sources[index]['source_index'] + 1}."))

    scores = []
    for source, trays in zip(sources, candidates):
        required = source.get('color')
        row = {}
        for tray in trays:
            color = hex_color(tray.get('color_hex'))
            exact = same_color(required, {'color_hex': color}) if color else same_color(required, tray)
            known = bool(required and color)
            row[tray['slot'] - 1] = (int(not exact), int(not exact and not known),
                                     color_distance(required, color) if known and not exact else 0.)
        scores.append(row)

    furthest = 0

    @lru_cache(None)
    def best(i, used):
        nonlocal furthest
        furthest = max(furthest, i)
        if i == len(scores):
            return (0, 0, 0.), ()
        optimum = None
        for target, cost in scores[i].items():
            bit = 1 << target
            if used & bit:
                continue
            tail = best(i + 1, used | bit)
            if tail is None:
                continue
            score = tuple(a + b for a, b in zip(cost, tail[0]))
            choices = (frozenset((target,)),) + tail[1]
            if optimum and score[:2] == optimum[0][:2] and isclose(score[2], optimum[0][2], abs_tol=1e-9):
                optimum = optimum[0], tuple(a | b for a, b in zip(optimum[1], choices))
            elif optimum is None or score < optimum[0]:
                optimum = score, choices
        return optimum

    result = best(0, 0)
    if result is None:
        filament = sources[furthest]['source_index'] + 1
        raise ValueError(RackMessage('ams_mapping_required', f'No free compatible position is left for filament {filament}.'))
    # A tray shared by different filaments across optimal plans needs an operator.
    # Interchangeable same-colour trays for just one filament are unambiguous.
    choices = result[1]
    vector = [-1] * (max(s['source_index'] for s in sources) + 1)
    for i, (source, targets) in enumerate(zip(sources, choices)):
        target = min(targets)
        if len(targets) > 1:
            if any(targets & other for j, other in enumerate(choices) if j != i):
                raise choose(i)
            exact = all(scores[i][t][0] == 0 for t in targets)
            if not exact:
                colors = {hex_color(tray.get('color_hex')) for tray in candidates[i]}
                if None in colors or len(colors) != 1:
                    raise choose(i)
            if selection_notes is not None:
                description = 'exact colour' if exact else 'loaded colour'
                selection_notes[source['source_index']] = f'Position {target + 1} is the lowest position with the same {description}.'
        vector[source['source_index']] = target
    return vector


def rack_preview(sources, available, installed, stored):
    candidates = rack_candidates(sources, available, installed)
    rows = []
    for source, trays in zip(sources, candidates):
        color = source.get('color')
        rows.append(dict(slot=source['source_index'] + 1, color=color or '', material=source['material'],
                         diameter=source['nozzle_diameter'], tool='TPU' if source['volume_type'] == 'TPU285' else 'standard',
                         eligible=[t['slot'] for t in trays],
                         different_colors=[t['slot'] for t in available if color_warning(source, t['slot'], t.get('color_hex'))]))
    result = dict(required=rows, available=available, suggested_mapping=[], rack_mapping=[])
    try:
        notes = {}
        mapping = stored
        if mapping is None:
            mapping = derive_mapping(sources, candidates, selection_notes=notes)
        for row in rows:
            if row['slot'] - 1 in notes:
                row['selection_note'] = notes[row['slot'] - 1]
        result['rack_mapping'] = [dict(source_slot=i+1, target_slot=n+1) for i, n in enumerate(mapping)
                                  if type(n) is int and n >= 0]
    except ValueError as exc:
        result['file_note'] = str(exc)
    return result
