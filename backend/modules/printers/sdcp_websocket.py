"""WebSocket settings for Elegoo SDCP printers under ITAR mode.

The adapter connects to the address that passed the device policy
(core.webhook_utils.device_connect_ip). websocket-client then follows up to three HTTP
redirects by itself: a printer on the local network that answers `302 Location:
ws://<public address>/...` would make ODIN connect there with no further check. Under ITAR
mode redirects are turned off, so the only connection is the one to the checked address.
With ITAR mode off nothing here changes how the library behaves.
"""
from core.itar import is_itar_mode


def connection_options() -> dict:
    """Keyword arguments for websocket.create_connection / WebSocket.connect."""
    return {"redirect_limit": 0} if is_itar_mode() else {}


def _make_app_sockets_check_itar() -> None:
    """WebSocketApp.run_forever builds its socket from the name `WebSocket` in
    websocket._app and passes it a fixed argument list with no redirect setting. Replace
    that name, once, with a subclass that adds connection_options() to every connect.
    It reads ITAR mode at each connect, so it does nothing with ITAR mode off."""
    import websocket._app as app_module

    base = app_module.WebSocket
    if getattr(base, "_odin_checks_itar", False):
        return

    class ItarAwareWebSocket(base):
        _odin_checks_itar = True

        def connect(self, url, **options):
            options.update(connection_options())
            return super().connect(url, **options)

    app_module.WebSocket = ItarAwareWebSocket


def websocket_app(url, **callbacks):
    """websocket.WebSocketApp(url, **callbacks) whose socket follows no redirect under ITAR mode."""
    import websocket

    try:
        _make_app_sockets_check_itar()
    except Exception:
        # The library no longer has the name this relies on (an upgrade). With ITAR mode off
        # nothing was going to change; under ITAR mode refuse instead of following redirects.
        if is_itar_mode():
            raise
    return websocket.WebSocketApp(url, **callbacks)
