"""Exact per-model Bambu lookups. Unknown codes never imply a device or fault."""
from urllib.parse import quote
from modules.printers.bambu_hms_bundle import canonical, load_bundle, credential_prefix


def display_code(code):
    value = canonical(code)
    return '-'.join(value[i:i + 4] for i in range(0, len(value), 4))


def wiki_url(code):
    return 'https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/' + quote(display_code(code).replace('-', '_'), safe='_')


def lookup_hms_code(code, model_prefix=''):
    value = canonical(code)
    table = 'device_hms' if len(value) == 16 else 'device_error'
    return load_bundle(model_prefix).get(table, {}).get(value, f'Printer code {display_code(code)}')


def lookup_print_error(code, model_prefix=''):
    value = canonical(code)
    return load_bundle(model_prefix).get('device_error', {}).get(value, f'Printer code {display_code(code)}')


def get_code_count(model_prefix=''):
    return sum(len(table) for table in load_bundle(model_prefix).values())


def parse_hms_errors(hms_data, model_prefix=''):
    errors = {}
    for item in hms_data if isinstance(hms_data, list) else []:
        if not isinstance(item, dict):
            continue
        attr, code = item.get('attr'), item.get('code')
        if any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in (attr, code)):
            continue
        full_code = f'{attr:08X}_{code:08X}'
        message = lookup_hms_code(full_code, model_prefix)
        if not message:
            continue
        level = code >> 16
        errors[full_code] = dict(code=full_code, attr=attr, raw_code=code,
            severity={1: 'critical', 2: 'error', 3: 'info', 4: 'info'}.get(level, 'unknown'),
            message=message, wiki_url=wiki_url(full_code))
    return list(errors.values())
