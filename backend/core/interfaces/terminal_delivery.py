"""Compose terminal notification and mobile delivery services lazily."""

def register_mobile_delivery(outbox=None):
    from modules.push.fanout import register_subscribers
    if outbox is None:
        from modules.notifications import alert_outbox as outbox
    register_subscribers(outbox)
