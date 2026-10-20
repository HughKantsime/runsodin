"""Closed dispatch between the three internally typed media protocols."""
def prepare(capability,argv):
    from core.media_process_capability import MediaEncodeCapability,prepare as encode
    from core.media_derivative_capability import MediaDerivativeCapability,prepare as derivative
    from core.media_probe_capability import MediaProbeCapability,prepare as probe
    if type(capability) is MediaProbeCapability:return probe(capability,argv)
    if type(capability) is MediaEncodeCapability:return encode(capability,argv)
    if type(capability) is MediaDerivativeCapability:return derivative(capability,argv)
    raise ValueError('Unsupported media capability')

def envelope_command(record):
    # Anchor imports this module by its core-directory path.
    if type(record) is not dict:raise ValueError('Invalid media envelope')
    if record.get('roles')==['guard','namespace','output']:
        from media_process_capability import validate_envelope,worker_command,MARKERS
    elif record.get('roles')==['guard','source','output']:
        from media_derivative_capability import validate_envelope,worker_command,MARKERS
    elif record.get('roles')==['guard','source']:
        from media_probe_capability import validate_envelope,worker_command,MARKERS
    else:raise ValueError('Unsupported media roles')
    return validate_envelope(record),worker_command(),MARKERS
