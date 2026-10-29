"""Observed Bambu tray identity. Callers own the inventory graph locks and commit."""
import json
import uuid


def decode(value):
    return json.loads(value) if value else {}


def tag(value):
    return value.strip().upper() if isinstance(value, str) and value.strip().strip('0') else None


def observation(tray):
    return dict(tag=tag(tray.get('tag_uid')), uuid=tag(tray.get('tray_uuid')),
                type=tray.get('_reported_type', tray.get('tray_type')) or '', sub_brand=tray.get('tray_sub_brands') or '',
                preset=tray.get('tray_info_idx') or '', color=(tray.get('tray_color') or '')[:6].upper(),
                exists=tray.get('_exists'))


def advance(previous, tray, bound_tag=None, bound_uuid=None):
    observed = observation(tray)
    old = previous.get('observed')
    if old:
        old = {**old, 'tag': tag(old.get('tag')), 'uuid': tag(old.get('uuid'))}
    # On upgrade the assigned RFID record is evidence of the previous identity.
    prior = old or dict(tag=tag(bound_tag), uuid=tag(bound_uuid))
    old_identity = prior.get('uuid') or prior.get('tag')
    new_identity = observed['uuid'] or observed['tag']
    same_rfid = bool(new_identity and (prior.get('uuid') == observed['uuid'] if prior.get('uuid') and observed['uuid']
                                     else prior.get('tag') and prior.get('tag') == observed['tag']))
    strong = bool((old_identity or new_identity) and not same_rfid)
    if old and not old_identity and not new_identity:
        strong = bool((tray.get('_removed') and observed['exists'] is True) or (old.get('exists') is False and observed['exists'] is True)
                      or (observed['exists'] is not False and old.get('type') != observed['type'] and old.get('color') != observed['color']))
    # Empty trays depart immediately, including tag-less spools.
    if observed['exists'] is False or not observed['type']:
        strong = True
    changed = old != observed
    pending = previous.get('pending', False)
    if strong or same_rfid or new_identity:
        pending = False
    elif observed['exists'] is not False and old and changed and any(old.get(k) != observed[k] for k in ('type', 'sub_brand', 'preset', 'color')):
        pending = True
    state = dict(id=uuid.uuid4().hex if changed or strong else previous['id'], observed=observed, pending=pending)
    if pending:
        state['hold_id'] = previous.get('hold_id', previous.get('id')) if previous.get('pending') else state['id']
    if not changed and previous.get('confirmation_id'):
        state['confirmation_id'] = previous['confirmation_id']
    remain = tray.get('remain')
    state['remaining'] = remain if new_identity and not pending and type(remain) in (int, float) and 0 <= remain <= 100 and observed['exists'] is not False else None
    return state, strong


def departure(previous, tray, bound_tag=None, bound_uuid=None):
    """Decide identity only; inventory_moves owns all departure side effects."""
    state, unbind = advance(previous, tray, bound_tag, bound_uuid)
    reason = None
    if unbind:
        if tray.get('_exists') is False or not tray.get('tray_type'):
            reason = 'empty'
        elif any((tag(bound_tag), tag(bound_uuid), state['observed']['tag'],
                  state['observed']['uuid'], previous.get('observed', {}).get('tag'),
                  previous.get('observed', {}).get('uuid'))):
            reason = 'rfid_changed'
        else:
            reason = 'tray_changed'
    return state, reason


def observe(run, slot_id, state):
    """Persist the pre-departure decision after RFID destinations are applied."""
    run('UPDATE filament_slots SET tray_observation=:state WHERE id=:id', dict(state=json.dumps(state), id=slot_id))
    return state


def preset_label(state):
    observed = state.get('observed', {})
    name = observed.get('sub_brand') or observed.get('type')
    if not name:
        return None
    name = name.removeprefix('Bambu Lab ').removeprefix('Bambu ')
    return 'preset: Bambu ' + name
