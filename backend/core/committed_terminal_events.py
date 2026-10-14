"""Deliver a committed physical completion without replaying its database write."""
import logging


def publish_committed_completion(printer_id, print_job_id, scheduled_job_id=None, *,
                                 metadata=None, source_module='printers'):
    if (type(printer_id) is not int or printer_id <= 0
            or type(print_job_id) is not int or print_job_id <= 0):
        return False
    if source_module not in ('printers', 'notifications'):
        return False
    data = {'printer_id': printer_id, 'print_job_id': print_job_id,
            'scheduled_job_id': scheduled_job_id,
            'status': 'completed', 'success': True}
    if metadata is not None:
        if type(metadata) is not dict or set(metadata) - {'job_name', 'duration_seconds'}:
            return False
        data.update(metadata)
    from core import events
    from core.event_bus import get_event_bus
    from core.interfaces.event_bus import Event

    try:
        get_event_bus().publish(Event(
            event_type=events.JOB_COMPLETED,
            source_module=source_module,
            data=data,
        ))
        return True
    except Exception:
        logging.getLogger('printers').warning(
            'Committed print completion event delivery failed; observation retained')
        return False
