"""Synchronous completion services for daemons and API modules."""

def next_suggestion(*args, **kwargs):
    from modules.jobs.completion_summary import next_suggestion as implementation
    return implementation(*args, **kwargs)


def build_summary(*args, **kwargs):
    from modules.jobs.completion_summary import build_summary as implementation
    return implementation(*args, **kwargs)


def configuration_key(*args, **kwargs):
    from modules.jobs.completion_summary import configuration_key as implementation
    return implementation(*args, **kwargs)


def finished_attempt(*args, **kwargs):
    from modules.jobs.completion_summary import finished_attempt as implementation
    return implementation(*args, **kwargs)


def order_services():
    from modules.orders import batch_production, assortment, square_store, show_fulfillment
    return batch_production, assortment, square_store, show_fulfillment
