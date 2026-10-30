"""Use real WebSocketApp framing/decoding with an in-memory transport."""
import pytest
from websocket import ABNF, WebSocketApp
from websocket._core import WebSocket
from websocket._exceptions import WebSocketProtocolException

from pytradekit.gateway.websocket.public_receiver import configure_public_receiver


class MemorySocket:
    def __init__(self, payload):
        self.payload = payload
        self.read_sizes = []
        self.sent = []

    def recv(self, size):
        self.read_sizes.append(size)
        value, self.payload = self.payload[:size], self.payload[size:]
        return value

    def send(self, value):
        self.sent.append(value)
        return len(value)

    def gettimeout(self):
        return None

    def pending(self):
        return 0

    def close(self):
        pass


def frame(payload, opcode=ABNF.OPCODE_TEXT, fin=1):
    return ABNF(fin=fin, opcode=opcode, mask=0, data=payload).format()


def run_stream(monkeypatch, payload, *, configure=True):
    transport = MemorySocket(payload)
    created = []
    class Core(WebSocket):
        def connect(self, *args, **kwargs):
            self.sock = transport
            self.connected = True
            created.append(self)
        def is_ssl(self):
            return True
        def close(self, *args, **kwargs):
            self.connected = False
    monkeypatch.setattr('websocket._app.WebSocket', Core)
    selections = []
    class Selector:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
        def register(self, *args):
            pass
        def select(self, timeout):
            selections.append(timeout)
            return [(object(), None)]
        def close(self):
            pass
    monkeypatch.setattr('selectors.DefaultSelector', Selector)
    delivered, failures, pings, closes = [], [], [], []
    app = WebSocketApp('wss://public.test/ws',
        on_message=lambda ws, value: delivered.append(value),
        on_error=lambda ws, error: failures.append(error),
        on_ping=lambda ws, value: pings.append(value),
        on_close=lambda ws, code, reason: closes.append((code, reason)))
    if configure:
        configure_public_receiver(app)
    app.run_forever()
    return transport, delivered, failures, pings, closes, selections, created


def test_burst_uses_bounded_read_ahead_and_drains_buffer_without_poll(monkeypatch):
    messages = [f'{{"u":{i},"s":"BTCUSDT"}}' for i in range(100)]
    payload = b''.join(frame(value.encode()) for value in messages)
    payload += frame(b'\x03\xe8', ABNF.OPCODE_CLOSE)
    transport, delivered, failures, _, closes, selections, _ = run_stream(monkeypatch, payload)
    assert delivered == messages
    assert not failures
    assert closes == [(1000, '')]
    assert len(transport.read_sizes) == 1
    assert max(transport.read_sizes) <= 16384
    assert len(selections) == 1


@pytest.mark.parametrize('payload', [b'\xff', b'\xc0\xaf', b'\xed\xa0\x80', b'\xf4\x90\x80\x80', b'\xe2\x82'])
def test_invalid_utf8_never_reaches_message_callback(monkeypatch, payload):
    _, delivered, failures, _, _, _, _ = run_stream(monkeypatch, frame(payload))
    assert not delivered
    assert len(failures) == 1


def test_fragmented_unicode_and_control_frames_keep_original_order(monkeypatch):
    payload = frame(b'\xe4', fin=0)
    payload += frame(b'heartbeat', ABNF.OPCODE_PING)
    payload += frame(b'\xb8\xad', ABNF.OPCODE_CONT)
    payload += frame(b'next') + frame(b'\x03\xe8bye', ABNF.OPCODE_CLOSE)
    transport, delivered, failures, pings, closes, _, _ = run_stream(monkeypatch, payload)
    assert delivered == ['中', 'next']
    assert pings == [b'heartbeat'] and transport.sent
    assert closes == [(1000, 'bye')] and not failures


def test_invalid_close_utf8_keeps_frame_validation(monkeypatch):
    _, delivered, failures, _, _, _, _ = run_stream(monkeypatch, frame(b'\x03\xe8\xff', ABNF.OPCODE_CLOSE))
    assert not delivered
    assert isinstance(failures[0], WebSocketProtocolException)


def test_large_burst_preserves_every_frame_with_bounded_reads(monkeypatch):
    messages = [str(i) + 'x' * 200 for i in range(1000)]
    payload = b''.join(frame(value.encode()) for value in messages) + frame(b'\x03\xe8', ABNF.OPCODE_CLOSE)
    transport, delivered, failures, _, _, _, _ = run_stream(monkeypatch, payload)
    assert delivered == messages and not failures
    assert max(transport.read_sizes) <= 16384
    assert len(transport.read_sizes) < 20
