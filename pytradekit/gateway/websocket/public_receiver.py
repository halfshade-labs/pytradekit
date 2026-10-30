"""Bounded read-ahead for websocket-client 1.3.1 public text streams.

The upstream frame parser, control validation and strict WebSocketApp UTF-8
decoder remain in charge. No frames are coalesced or dropped by this adapter.
"""
import selectors
from typing import Callable, Optional

from websocket import WebSocketApp

READ_AHEAD_BYTES = 16384
POLL_INTERVAL_S = 0.1


class _PublicDispatcher:
    def __init__(self, app: WebSocketApp, timeout: Optional[float]) -> None:
        self.app = app
        self.timeout = min(timeout or POLL_INTERVAL_S, POLL_INTERVAL_S)

    def read(self, sock: object, read_callback: Callable[[], bool],
             check_callback: Callable[[], bool]) -> None:
        connection = self.app.sock
        frames = connection.frame_buffer
        receive = frames.recv
        # recv_strict already retains surplus bytes and splits at frame bounds.
        frames.recv = lambda size: receive(READ_AHEAD_BYTES)
        # Only bypass the duplicate Python byte scan for complete text messages.
        # WebSocketApp strictly decodes their reassembled bytes before callbacks;
        # frame_buffer.skip_utf8_validation stays False, including CLOSE reasons.
        connection.cont_frame.skip_utf8_validation = True
        pending = getattr(sock, 'pending', lambda: 0)
        with selectors.DefaultSelector() as selector:
            selector.register(sock, selectors.EVENT_READ)
            while self.app.keep_running:
                ready = bool(frames.recv_buffer) or bool(pending())
                if not ready:
                    ready = bool(selector.select(self.timeout))
                if ready and not read_callback():
                    break
                check_callback()


def configure_public_receiver(app: WebSocketApp) -> bool:
    """Configure one public socket; never patch websocket-client globally."""
    if not callable(getattr(app, 'create_dispatcher', None)):
        return False
    original = app.create_dispatcher

    def create_dispatcher(ping_timeout: Optional[float], dispatcher: object = None) -> object:
        if dispatcher is not None or app.on_cont_message is not None:
            return original(ping_timeout, dispatcher)
        return _PublicDispatcher(app, ping_timeout)

    app.create_dispatcher = create_dispatcher
    return True
