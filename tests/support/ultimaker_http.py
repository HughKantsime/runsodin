"""TEST ONLY: transcripts for the Ultimaker adapter and monitor tests.

Built from Ultimaker's public local API documentation. Route them with the fake transport in
tests.support.octoprint_http (Octo, Reply) and tests.support.device_http.route_device_requests.
"""

UUID = '0b7f6f3a-1c2d-4e5f-8a9b-0c1d2e3f4a5b'
P = '/api/v1/printer'
J = '/api/v1/print_job'
S = '/api/v1/system'


def printer_body(status='printing', bed=(60.0, 60.0), extruders=((215.0, 215.0),)):
    return {
        'status': status,
        'bed': {'temperature': {'current': bed[0], 'target': bed[1]}},
        'heads': [{'extruders': [
            {'hotend': {'temperature': {'current': current, 'target': target}},
             'active_material': {'guid': f'guid-{index}'}}
            for index, (current, target) in enumerate(extruders)]}],
    }


def job_body(state='printing', name='gear', progress=0.42, elapsed=600, total=1440, uuid=UUID):
    return {'name': name, 'state': state, 'progress': progress,
            'time_elapsed': elapsed, 'time_total': total, 'uuid': uuid}


def system_body(variant='Ultimaker S5', firmware='8.3.0.20240617'):
    return {'name': 'Workshop S5', 'variant': variant, 'firmware': firmware,
            'guid': '11111111-2222-3333-4444-555555555555'}
