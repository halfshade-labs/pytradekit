"""Explicit, bounded native child ownership. No automatic Python fallback."""
import hashlib
import io
import json
import os
import queue
import select
import subprocess
import threading
from pathlib import Path
from pytradekit.utils.time_handler import get_monotonic_timestamp_ns


class NativePublicMarket:
    def __init__(self, binary, sha256, header, on_delta, *, owner_completion_receipts=False, owner_retirement=False, live_accounting=False, admission_deltas=False, feed_views=False):
        if type(owner_completion_receipts) is not bool:
            raise ValueError("owner_completion_receipts must be bool")
        if type(owner_retirement) is not bool or (owner_retirement and not owner_completion_receipts):
            raise ValueError("owner_retirement requires bool and completion receipts")
        if type(live_accounting) is not bool or (live_accounting and not (owner_retirement and owner_completion_receipts)):
            raise ValueError("live_accounting requires bool, retirement and completion receipts")
        if type(admission_deltas) is not bool or (admission_deltas and not (
                owner_completion_receipts and owner_retirement and live_accounting)):
            raise ValueError("admission_deltas requires bool, receipts, retirement and live accounting")
        if type(feed_views) is not bool or (feed_views and not (
                admission_deltas and live_accounting and owner_retirement and owner_completion_receipts)):
            raise ValueError("feed_views requires bool, admission deltas, live accounting, retirement and receipts")
        if admission_deltas and (type(header.get("version")) is not int or header["version"] != 3
                or header.get("configuration_transactions") is not True
                or type(header.get("native")) is not dict or header.get("diagnostics") is not False
                or header.get("native_event_diagnostics", False) is not False):
            raise ValueError("admission_deltas requires configured native mode without diagnostics")
        if owner_retirement and (header.get("version") != 3 or header.get("configuration_transactions") is not True):
            raise ValueError("owner retirement requires configured whole-plan live mode")
        path = Path(binary).resolve(strict=True)
        if not path.is_file() or len(sha256) != 64:
            raise ValueError("native binary and verified SHA256 required")
        with path.open("rb") as source:
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(65536), b""):
                digest.update(chunk)
            actual = digest.hexdigest()
        if actual != sha256.lower():
            raise ValueError("native binary identity mismatch")
        self.session = header["native"]["session"]
        self._owner_completion_receipts = owner_completion_receipts
        self._owner_retirement = owner_retirement
        self._live_accounting = live_accounting
        self._feed_views = feed_views
        self._admission_deltas = admission_deltas
        self._receipt_ready = False
        self._on_delta = on_delta
        self._lock = threading.RLock()
        self._failure_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._failure = None
        self._closed = False
        self._next = 0
        self._requests = {}
        self._request_metadata = {}
        self._ready = threading.Event()
        self._complete = False
        self._child = None
        try:
            arguments = [str(path), "--live"]
            if owner_completion_receipts:
                arguments.append("--owner-completion-receipts=v1")
            if owner_retirement:
                arguments.append("--owner-retirement=v1")
            if live_accounting:
                arguments.append("--live-accounting=v1")
            if admission_deltas:
                arguments.append("--admission-deltas=v1")
            if feed_views:
                arguments.append("--feed-views=v1")
            self._child = subprocess.Popen(arguments, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           bufsize=0, close_fds=True)
            os.set_blocking(self._child.stdin.fileno(), False)
            self._reader = threading.Thread(target=self._read, name="native-market-deltas", daemon=True)
            self._reader.start()
            self.send(header)
        except BaseException:
            try:
                self.close()
            except BaseException as cleanup_error:
                self.close_cleanup_error = self._failure_text(cleanup_error)
            raise

    def _fail(self, reason):
        self._fail_receipt(reason)

    def _fail_receipt(self, reason):
        # Never wait for the registry to make the receipt client unavailable.
        # This private lock only protects the first scalar failure assignment;
        # diagnostic formatting and all registry work occur outside it.
        diagnostic = self._failure_text(reason)
        if not hasattr(self, "_failure_lock"):
            self._failure_lock = threading.Lock()
        with self._failure_lock:
            if self._failure is None:
                self._failure = diagnostic
        if self._lock.acquire(blocking=False):
            try:
                self._drain_owner_completions()
                for target in self._requests.values():
                    try:
                        target.put_nowait({"accepted": False})
                    except queue.Full:
                        pass
            finally:
                self._lock.release()

    def _drain_owner_completions(self):
        # Caller owns the registry lock. Timed-out cleanup retains its original
        # shared request slot (maximum 64), never a second queue or worker.
        for request, metadata in tuple(getattr(self, "_request_metadata", {}).items()):
            retired = metadata.get("retired")
            if retired is not None and retired.is_set():
                self._requests.pop(request, None)
                self._request_metadata.pop(request, None)

    @staticmethod
    def _failure_text(reason):
        try:
            text = str(reason)
            if not text.strip():
                text = f"native failure ({type(reason).__name__}): no diagnostic text"
            bounded = text[:256]
            return bounded if bounded.strip() else text.lstrip()[:256] or "native failure"
        except BaseException:
            return "native failure: diagnostic formatting failed"

    def _encode_control(self, value):
        data = (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode()
        configuration = str(value.get("type", "")).startswith("configuration_")
        if configuration and len(data) > 16384:
            raise ValueError("native configuration frame exceeds 16KiB bound")
        if len(data) > 65536:
            self._fail("native control frame exceeds bound")
            raise RuntimeError(self._failure)
        if len(data) > 16384:
            raise ValueError("native control frame exceeds 16KiB bound")
        return data

    def send(self, value):
        deadline = get_monotonic_timestamp_ns() + 200_000_000
        metadata = getattr(self, "_request_metadata", {}).get(value.get("request"))
        if metadata is not None and "deadline" in metadata:
            deadline = min(deadline, metadata["deadline"])
        if self._failure is not None or self._closed:
            raise RuntimeError(self._failure if self._failure is not None else "native child unavailable")
        # Only this slot's original outgoing object may reuse its validated frame.
        # External sends still encode their own values, including the same ID.
        data = (metadata["encoded_control"] if metadata is not None and metadata.get("outgoing") is value
                else self._encode_control(value))
        remaining = max(0, deadline - get_monotonic_timestamp_ns()) / 1_000_000_000
        if not self._lock.acquire(timeout=remaining):
            self._fail("native control registration deadline")
            raise RuntimeError(self._failure)
        try:
            self._drain_owner_completions()
            if self._failure is not None or self._closed:
                raise RuntimeError(self._failure if self._failure is not None else "native child unavailable")
        finally:
            self._lock.release()
        try:
            self._write_control(data, deadline, "native control")
        except BaseException as error:
            self._fail(error if isinstance(error, (OSError, RuntimeError)) else "native control cancelled")
            if isinstance(error, TimeoutError):
                raise RuntimeError(self._failure) from error
            raise

    @staticmethod
    def _raw_readline(source):
        # FileIO.read performs one raw read, without BufferedReader's close lock.
        # Decorated readline streams retain their observers and original API.
        pending = bytearray()
        def readline(limit):
            if source.closed:
                raise ValueError("I/O operation on closed file")
            while True:
                newline = pending.find(b"\n", 0, limit)
                if newline >= 0 or len(pending) >= limit:
                    end = newline + 1 if newline >= 0 else limit
                    result = bytes(pending[:end])
                    del pending[:end]
                    return result
                chunk = source.read(min(65536, limit - len(pending)))
                if not chunk:
                    result = bytes(pending)
                    pending.clear()
                    return result
                pending.extend(chunk)
        return readline

    def _read(self):
        try:
            source = self._child.stdout
            readline = self._raw_readline(source) if isinstance(source, io.RawIOBase) else source.readline
            while True:
                raw = readline(1048577)
                if not raw:
                    break
                if len(raw) > 1048576 or not raw.endswith(b"\n"):
                    raise RuntimeError("native delta frame exceeds bound")
                row = json.loads(raw)
                kind = row.get("type")
                if kind in ("validation", "snapshot", "configuration", "control_ack", "owner_completion") or (
                        kind == "feed_view" and getattr(self, "_feed_views", False)):
                    if kind == "owner_completion" and not getattr(self, "_owner_completion_receipts", False):
                        raise RuntimeError("unsolicited native owner completion")
                    with self._lock:
                        if getattr(self, "_owner_completion_receipts", False):
                            self._drain_owner_completions()
                        request = row.get("request")
                        target = self._requests.get(request) if type(request) is int else None
                        metadata = getattr(self, "_request_metadata", {}).get(request)
                    if target is None:
                        raise RuntimeError("unknown or late native validation response")
                    if metadata is not None and kind != metadata["kind"]:
                        raise RuntimeError("conflicting native response kind")
                    if kind == "feed_view":
                        self._check_feed_view(row, metadata, request)
                        with self._lock:
                            if metadata.get("delivered"):
                                raise RuntimeError("duplicate native feed view")
                            metadata["delivered"] = True
                    elif kind == "configuration":
                        self._check_configuration_response(row, metadata)
                        with self._lock:
                            if metadata.get("delivered"):
                                raise RuntimeError("duplicate native configuration response")
                            metadata["delivered"] = True
                    elif kind == "owner_completion":
                        self._check_owner_completion(row, metadata)
                        with self._lock:
                            if metadata.get("delivered"):
                                raise RuntimeError("duplicate native owner completion")
                            metadata["delivered"] = True
                    elif kind == "validation" and metadata is not None and metadata.get("owner_validation"):
                        self._check_owner_validation(row, metadata)
                        with self._lock:
                            if metadata.get("delivered"):
                                raise RuntimeError("duplicate native owner validation")
                            metadata["delivered"] = True
                    elif kind == "control_ack" and metadata is None:
                        raise RuntimeError("unexpected native control acknowledgment")
                    target.put_nowait(row)
                    if kind == "configuration" and row["phase"] == "FAILED":
                        self._fail("native configuration failed: " + str(row.get("error")))
                elif kind == "delta":
                    if getattr(self, "_admission_deltas", False):
                        observations = row.get("observations")
                        if (not self._receipt_ready or type(observations) is not list or not observations
                                or any(type(state) is not dict or set(state) != {"admissions"}
                                       or type(state["admissions"]) is not list or not state["admissions"]
                                       for state in observations)):
                            raise RuntimeError("invalid native admission delta observations")
                    if self._failure is None:
                        self._on_delta(row)
                elif kind == "complete":
                    self._complete = True
                elif kind == "failure":
                    self.failure_profile = row
                    self._fail("native owner failure: " + str(row.get("reason", "unknown"))[:256])
                elif kind == "ready":
                    if row.get("version") != 3 or row.get("production_permission") is not False:
                        raise RuntimeError("invalid native readiness")
                    if getattr(self, "_owner_completion_receipts", False):
                        capability = row.get("owner_completion_receipts")
                        if (type(row.get("version")) is not int or type(capability) is not dict
                                or set(capability) != {"version", "session"}
                                or type(capability.get("version")) is not int or capability["version"] != 1
                                or capability.get("session") != self.session or self._receipt_ready):
                            raise RuntimeError("invalid native owner completion capability")
                        if getattr(self, "_owner_retirement", False):
                            retirement = row.get("owner_retirement")
                            if (type(retirement) is not dict or set(retirement) != {"version", "session"}
                                    or type(retirement.get("version")) is not int or retirement["version"] != 1
                                    or type(retirement.get("session")) is not str or retirement["session"] != self.session):
                                raise RuntimeError("invalid native owner retirement capability")
                        if getattr(self, "_live_accounting", False):
                            accounting = row.get("live_accounting")
                            if (type(accounting) is not dict or set(accounting) != {"version", "session"}
                                    or type(accounting.get("version")) is not int or accounting["version"] != 1
                                    or type(accounting.get("session")) is not str or accounting["session"] != self.session):
                                raise RuntimeError("invalid native live accounting capability")
                        if getattr(self, "_admission_deltas", False):
                            deltas = row.get("admission_deltas")
                            if (type(deltas) is not dict or set(deltas) != {"version", "session"}
                                    or type(deltas.get("version")) is not int or deltas["version"] != 1
                                    or type(deltas.get("session")) is not str or deltas["session"] != self.session):
                                raise RuntimeError("invalid native admission deltas capability")
                        if getattr(self, "_feed_views", False):
                            views = row.get("feed_views")
                            if (type(views) is not dict or set(views) != {"version", "session"}
                                    or type(views.get("version")) is not int or views["version"] != 1
                                    or type(views.get("session")) is not str or views["session"] != self.session):
                                raise RuntimeError("invalid native feed views capability")
                        self._receipt_ready = True
                    ready = getattr(self, "_ready", None)
                    if ready is not None:
                        ready.set()
                else:
                    raise RuntimeError("unknown native output")
            if not self._closed:
                raise RuntimeError("native child EOF while active")
        except BaseException as error:
            self._fail(error)

    def _check_configuration_response(self, row, metadata):
        fields = {"type", "request", "session", "operation", "revision", "plan_digest", "phase", "accepted", "error"}
        if (metadata is None or set(row) != fields or row.get("session") != self.session
                or type(row.get("accepted")) is not bool or type(row.get("revision")) is not int
                or not 0 <= row["revision"] <= 2**64-1 or row.get("phase") not in
                {"PREPARED", "COMMITTED", "APPLYING", "READY", "ACTIVE", "ABORTED", "REJECTED", "FAILED"}
                or row.get("error") is not None and
                (not isinstance(row["error"], str) or len(row["error"]) > 256)):
            raise RuntimeError("invalid native configuration response")
        expected = metadata["identity"]
        rejected = (row["accepted"] is False and row["phase"] == "REJECTED"
                    and row["operation"] is None and row["plan_digest"] is None
                    and row["revision"] in {expected["revision"], expected["expected_revision"]})
        matched = (type(row.get("operation")) is int and 0 < row["operation"] <= 2**64-1
                   and isinstance(row.get("plan_digest"), str) and len(row["plan_digest"]) == 64
                   and all(c in "0123456789abcdefABCDEF" for c in row["plan_digest"])
                   and all(row.get(key) == expected[key] for key in ("operation", "revision", "plan_digest")))
        if (row["phase"] in {"REJECTED", "FAILED"} and row["accepted"] is not False
                or row["phase"] in {"PREPARED", "COMMITTED", "APPLYING", "READY", "ACTIVE"} and row["accepted"] is not True):
            raise RuntimeError("invalid native configuration acceptance")
        if not (rejected or matched):
            raise RuntimeError("conflicting native configuration identity")

    def wait_ready(self, timeout=10):
        return self._ready.wait(timeout) and self._failure is None and not self._closed

    @staticmethod
    def _completion_identity(value):
        fields = {"session", "candidate", "symbol", "venue", "configuration_revision"}
        if type(value) is not dict or not fields <= value.keys():
            raise ValueError("invalid native owner completion identity")
        identity = {key: value[key] for key in fields}
        session, symbol = identity["session"], identity["symbol"]
        if (type(session) is not str or not 1 <= len(session) <= 40
                or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for c in session)
                or type(symbol) is not str or not 1 <= len(symbol) <= 32
                or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in symbol)
                or type(identity["venue"]) is not str or identity["venue"] not in {"BN", "HTX", "OKX"}
                or type(identity["candidate"]) is not int or not 0 < identity["candidate"] <= 2**64-1
                or type(identity["configuration_revision"]) is not int
                or not 0 <= identity["configuration_revision"] <= 2**64-1):
            raise ValueError("invalid native owner completion identity")
        return identity

    @staticmethod
    def _completion_result(value):
        if type(value) is not dict or set(value) != {"phase", "result"} or type(value["result"]) is not dict:
            raise ValueError("invalid native owner completion result")
        phase, result = value["phase"], value["result"]
        if phase == "worker":
            valid = (set(result) == {"outcome"} and type(result["outcome"]) is str
                     and result["outcome"] in {"error", "expired", "invalidated"})
        elif phase == "publication":
            valid = (set(result) == {"success", "completed_ms"} and type(result["success"]) is bool
                     and (result["completed_ms"] is None if result["success"] is False else
                          type(result["completed_ms"]) is int and 0 < result["completed_ms"] <= 4000000000000))
        else:
            valid = False
        if not valid:
            raise ValueError("invalid native owner completion result")
        return {"phase": phase, "result": dict(result)}

    def _check_owner_completion(self, row, metadata):
        fields = {"type", "request", "session", "candidate", "symbol", "venue", "configuration_revision",
                  "phase", "result", "accepted", "recorded", "terminal", "error"}
        try:
            identity = self._completion_identity(row)
            completion = self._completion_result({"phase": row["phase"], "result": row["result"]})
            valid = (metadata is not None and metadata["kind"] == "owner_completion"
                     and get_monotonic_timestamp_ns() <= metadata["deadline"]
                     and set(row) == fields and type(row["request"]) is int and 0 < row["request"] <= 2**64-1
                     and identity == metadata["identity"] and completion == metadata["completion"]
                     and all(type(row[key]) is bool for key in ("accepted", "recorded", "terminal")))
            if valid and row["accepted"]:
                valid = row["recorded"] and row["terminal"] and row["error"] is None
            elif valid:
                valid = (row["recorded"] is False and type(row["error"]) is str and 0 < len(row["error"]) <= 256)
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise RuntimeError("invalid native owner completion response")

    def _send_owner_completion(self, data, deadline):
        self._write_control(data, deadline, "native owner completion")

    def _write_control(self, data, deadline, label):
        remaining = max(0, deadline - get_monotonic_timestamp_ns()) / 1_000_000_000
        if not self._write_lock.acquire(timeout=remaining):
            raise TimeoutError(label + " write-lock deadline")
        try:
            offset = 0
            while offset < len(data):
                if self._failure is not None or self._closed or self._child.poll() is not None:
                    raise RuntimeError(self._failure if self._failure is not None else "native child unavailable")
                remaining = (deadline - get_monotonic_timestamp_ns()) / 1_000_000_000
                if remaining <= 0:
                    raise TimeoutError(label + " pipe deadline")
                if select.select([], [self._child.stdin], [], remaining)[1]:
                    try:
                        offset += os.write(self._child.stdin.fileno(), data[offset:])
                    except BlockingIOError:
                        continue
        finally:
            self._write_lock.release()

    def validate_owner(self, owner, phase):
        """Return strict acceptance separately from local snapshot qualification."""
        deadline = get_monotonic_timestamp_ns() + 250_000_000
        identity = self._completion_identity(owner)
        if type(phase) is not str or phase not in {"admission", "post_l2", "publication"}:
            raise ValueError("invalid native owner validation phase")
        canonical = json.dumps(owner, separators=(",", ":"), sort_keys=True, allow_nan=False)
        if len(canonical.encode()) > 1048576:
            raise ValueError("native owner validation envelope exceeds output bound")
        control = {"type": "validate", **{key: identity[key] for key in ("candidate", "symbol", "venue", "session")},
                   "phase": phase}
        metadata = {"kind": "validation", "owner_validation": True, "identity": identity,
                    "owner_json": canonical, "phase": phase}
        return self._owner_rpc(control, metadata, deadline)

    def _check_owner_validation(self, row, metadata):
        fields = {"type", "request", "owner", "accepted", "market_snapshot"}
        try:
            valid = (set(row) == fields and row["type"] == "validation" and metadata["kind"] == "validation"
                     and metadata["owner_validation"] is True
                     and metadata["phase"] in {"admission", "post_l2", "publication"}
                     and type(row["request"]) is int and 0 < row["request"] <= 2**64-1
                     and get_monotonic_timestamp_ns() <= metadata["deadline"]
                     and type(row["accepted"]) is bool and type(row["owner"]) is dict
                     and self._completion_identity(row["owner"]) == metadata["identity"]
                     and json.dumps(row["owner"], separators=(",", ":"), sort_keys=True, allow_nan=False) == metadata["owner_json"]
                     and (type(row["market_snapshot"]) is dict or row["market_snapshot"] is None))
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise RuntimeError("invalid native owner validation response")

    def complete_owner(self, owner, completion):
        """Record one existing native lease outcome; never grants publication permission."""
        deadline = get_monotonic_timestamp_ns() + 250_000_000
        identity = self._completion_identity(owner)
        completion = self._completion_result(completion)
        return self._owner_rpc({"type": "owner_complete", **identity, **completion},
                               {"kind": "owner_completion", "identity": identity, "completion": completion}, deadline)

    @staticmethod
    def _encode_owner_control(control, request):
        data = (json.dumps({**control, "request": request},
                           separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(data) > 16384:
            raise ValueError("native owner completion frame exceeds 16KiB bound")
        return data

    def _owner_rpc(self, control, expected, deadline):
        identity = expected["identity"]
        if identity["session"] != self.session:
            raise ValueError("conflicting native owner completion session")
        if not getattr(self, "_owner_completion_receipts", False) or not getattr(self, "_receipt_ready", False):
            raise RuntimeError("native owner completion capability unavailable")
        label = "native feed view" if expected["kind"] == "feed_view" else (
            "native owner validation" if expected["kind"] == "validation" else "native owner completion")
        # Local encoding has no transport uncertainty and consumes no shared slot.
        self._encode_owner_control(control, 1)
        request = None
        metadata = None
        registration_started = False
        local_error = None
        result = {}
        acquired = False
        cancelled = False
        try:
            response = queue.Queue(maxsize=1)
            metadata = {**expected, "delivered": False, "deadline": deadline, "retired": threading.Event()}
            remaining = max(0, deadline - get_monotonic_timestamp_ns()) / 1_000_000_000
            acquired = self._lock.acquire(timeout=remaining)
            if not acquired:
                raise TimeoutError(label + " registration deadline")
            try:
                self._drain_owner_completions()
                if self._failure is not None or self._closed or len(self._requests) >= 64:
                    return {}
                if not 0 <= self._next < 2**64-1:
                    raise RuntimeError("native request identity exhausted")
                self._next += 1
                request = self._next
                try:
                    data = self._encode_owner_control(control, request)
                except (TypeError, ValueError) as error:
                    local_error = error
                    raise
                # Publish retirement metadata first, so cancellation on either
                # dictionary store can retire the same bounded shared slot.
                registration_started = True
                self._request_metadata[request] = metadata
                self._requests[request] = response
            finally:
                self._lock.release()
                acquired = False
            self._send_owner_completion(data, deadline)
            remaining = max(0, deadline - get_monotonic_timestamp_ns()) / 1_000_000_000
            result = response.get(timeout=remaining)
            if self._failure is not None:
                result = {}
            if get_monotonic_timestamp_ns() > deadline:
                raise TimeoutError(label + " reply deadline")
        except (TypeError, ValueError) as error:
            if error is local_error:
                raise
            self._fail(error)
            result = {}
        except (OSError, RuntimeError, queue.Empty) as error:
            self._fail(error if not isinstance(error, queue.Empty) else label + " deadline")
            result = {}
        except BaseException:
            cancelled = True
            self._fail(label + " cancelled")
            raise
        finally:
            if acquired:
                self._lock.release()
            if registration_started:
                try:
                    if not self._finish_owner_completion(metadata, deadline):
                        result = {}
                except BaseException as cleanup_error:
                    self.owner_completion_cleanup_error = self._failure_text(cleanup_error)
                    if not cancelled:
                        self._fail("native owner completion cleanup failed")
                        raise
        if self._failure is not None or self._closed:
            return {}
        if get_monotonic_timestamp_ns() > deadline:
            self._fail(label + " deadline")
            return {}
        return result

    def _finish_owner_completion(self, metadata, deadline):
        metadata["retired"].set()
        remaining = max(0, deadline - get_monotonic_timestamp_ns()) / 1_000_000_000
        if not self._lock.acquire(timeout=remaining):
            self._fail("native owner completion cleanup deadline")
            return False
        try:
            self._drain_owner_completions()
            return True
        finally:
            self._lock.release()

    def configuration(self, control):
        if control.get("type") not in {"configuration_prepare", "configuration_commit", "configuration_status",
                                       "configuration_activate", "configuration_abort"}:
            raise ValueError("unknown native configuration command")
        return self.request(control)

    def validate(self, owner, phase):
        result = self.request({"type": "validate", "candidate": owner["candidate"],
                               "symbol": owner["symbol"], "venue": owner["venue"],
                               "session": self.session, "phase": phase})
        if result.get("accepted") is True and result.get("owner") == owner:
            return result.get("market_snapshot")
        return None

    def feed_view(self, symbol=None, *, spot=False):
        """Read one original Snapshot-selected leg; confers no owner permission."""
        deadline = get_monotonic_timestamp_ns() + 250_000_000
        if type(spot) is not bool or (symbol is not None and type(symbol) is not str):
            raise ValueError("invalid native feed view scope")
        if not getattr(self, "_feed_views", False) or not getattr(self, "_receipt_ready", False):
            raise RuntimeError("native feed views capability unavailable")
        identity = {"session": self.session, "leg": "spot" if spot else "feed", "symbol": symbol}
        # A character consumes at least one wire byte. Use the smallest legal
        # request ID to reject only scopes that cannot fit any valid frame,
        # before the shared helper serializes potentially enormous input.
        if symbol is not None:
            minimum = len((json.dumps({"type": "feed_view", **identity, "symbol": "", "request": 1},
                                      separators=(",", ":"), allow_nan=False) + "\n").encode())
            if len(symbol) > 16384 - minimum:
                raise ValueError("native feed view scope exceeds 16KiB bound")
        return self._owner_rpc({"type": "feed_view", **identity},
                               {"kind": "feed_view", "identity": identity}, deadline)

    def _check_feed_view(self, row, metadata, request):
        fields = {"type", "request", "session", "leg", "symbol", "state"}
        if (metadata is None or set(row) != fields or metadata["kind"] != "feed_view"
                or row["type"] != "feed_view" or type(row["request"]) is not int
                or not 0 < row["request"] <= 2**64-1 or row["request"] != request
                or type(row["session"]) is not str or row["session"] != self.session
                or type(row["leg"]) is not str or row["leg"] not in {"feed", "spot"}
                or (row["symbol"] is not None and type(row["symbol"]) is not str)
                or any(row[key] != metadata["identity"][key] for key in ("session", "leg", "symbol"))
                or type(row["state"]) is not dict
                or get_monotonic_timestamp_ns() > metadata["deadline"]):
            raise RuntimeError("invalid native feed view response")

    def snapshot(self, symbol=None):
        control = {"type": "snapshot"}
        if symbol is not None:
            control["symbol"] = symbol
        return self.request(control)

    def request(self, control):
        deadline = get_monotonic_timestamp_ns() + 250_000_000
        # Keep caller encoding errors outside the cancellation/transport fence.
        try:
            self._encode_control({**control, "request": 1})
        except (ValueError, RuntimeError):
            return {}
        request = metadata = None
        registration_started = False
        local_error = None
        result = {}
        cancelled = False
        exhausted = False
        try:
            response = queue.Queue(maxsize=1)
            kind = control.get("type")
            metadata = {"kind": {"validate": "validation", "snapshot": "snapshot",
                                "fixture_anchor": "control_ack"}.get(kind), "deadline": deadline,
                        "delivered": False, "retired": threading.Event()}
            if str(kind).startswith("configuration_"):
                identity = control.get("plan", control)
                metadata.update(kind="configuration", identity={
                    "operation": identity.get("operation"), "revision": identity.get("revision"),
                    "plan_digest": identity.get("plan_digest"),
                    "expected_revision": identity.get("expected_revision", identity.get("revision"))})
            remaining = max(0, deadline - get_monotonic_timestamp_ns()) / 1_000_000_000
            if not self._lock.acquire(timeout=remaining):
                raise TimeoutError("native request registration deadline")
            try:
                self._drain_owner_completions()
                if self._failure is not None or self._closed or len(self._requests) >= 64:
                    return {}
                if getattr(self, "_live_accounting", False) and not 0 <= self._next < 2**64-1:
                    exhausted = True
                    self._fail("native request identity exhausted")
                    raise RuntimeError("native request identity exhausted")
                self._next += 1
                request = self._next
                outgoing = {**control, "request": request}
                try:
                    data = self._encode_control(outgoing)
                except (TypeError, ValueError) as error:
                    local_error = error
                    raise
                # The existing 64-slot/16KiB bounds also bound these frozen bytes.
                metadata.update(outgoing=outgoing, encoded_control=data)
                if not hasattr(self, "_request_metadata"):
                    self._request_metadata = {}
                registration_started = True
                self._request_metadata[request] = metadata
                self._requests[request] = response
            finally:
                self._lock.release()
            self.send(outgoing)
            remaining = max(0, deadline - get_monotonic_timestamp_ns()) / 1_000_000_000
            result = response.get(timeout=remaining)
            if get_monotonic_timestamp_ns() > deadline:
                raise TimeoutError("native request reply deadline")
        except (TypeError, ValueError) as error:
            if error is local_error:
                if isinstance(error, TypeError):
                    raise
            else:
                self._fail(error)
            result = {}
        except (OSError, RuntimeError, queue.Empty, KeyError):
            if exhausted:
                raise
            self._fail("native owner validation failed")
            result = {}
        except BaseException:
            cancelled = True
            self._fail("native owner validation cancelled")
            raise
        finally:
            # Sending has ended; retired slots must not retain caller graphs.
            if metadata is not None:
                metadata.pop("outgoing", None)
                metadata.pop("encoded_control", None)
            if registration_started:
                try:
                    if not self._finish_owner_completion(metadata, deadline):
                        result = {}
                except BaseException as cleanup_error:
                    self.owner_completion_cleanup_error = self._failure_text(cleanup_error)
                    if not cancelled:
                        self._fail("native request cleanup failed")
                        raise
        if self._failure is not None or self._closed:
            return {}
        if get_monotonic_timestamp_ns() > deadline:
            self._fail("native request deadline")
            return {}
        return result

    def close(self):
        if getattr(self, "_closed", False) and getattr(self, "_close_cleanup_complete", False):
            return
        self._closed = True
        original = None
        original_traceback = None
        def remember(error):
            nonlocal original, original_traceback
            if original is None:
                original, original_traceback = error, error.__traceback__
            else:
                self.close_cleanup_error = self._failure_text(error)
        # Registry contention cannot postpone exact-owned process/FD cleanup.
        # Failure fencing and the closed scalar require no registry ownership.
        registry = getattr(self, "_lock", None)
        acquired = False
        registry_empty = False
        try:
            acquired = registry is not None and registry.acquire(timeout=.2)
            if acquired:
                self._drain_owner_completions()
                registry_empty = not getattr(self, "_requests", {}) and not getattr(self, "_request_metadata", {})
        except BaseException as error:
            remember(error)
        finally:
            if acquired:
                registry.release()
        child = getattr(self, "_child", None)
        if child is None:
            self._close_cleanup_complete = registry_empty
            if original is not None:
                raise original.with_traceback(original_traceback)
            return
        descriptors_closed = True
        try:
            if child.stdin:
                child.stdin.close()
        except BaseException as error:
            descriptors_closed = False
            remember(error)
        reaped = False
        try:
            child.wait(timeout=7)
        except subprocess.TimeoutExpired:
            pass
        except BaseException as error:
            remember(error)
        else:
            reaped = True
        # Only this exact Popen child is ever signalled. Cancellation of one
        # stage still attempts the remaining bounded cleanup stages.
        if not reaped:
            try:
                child.terminate()
            except BaseException as error:
                remember(error)
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            except BaseException as error:
                remember(error)
            else:
                reaped = True
            if not reaped:
                try:
                    child.kill()
                except BaseException as error:
                    remember(error)
                    # A cancellation may precede kill's signal side effect.
                    # Retry once only after this exact child still polls live.
                    try:
                        if child.poll() is None:
                            child.kill()
                    except BaseException as cleanup_error:
                        remember(cleanup_error)
                try:
                    child.wait(timeout=2)
                except BaseException as error:
                    remember(error)
                else:
                    reaped = True
        # Preserve terminal bytes for the owned reader before closing its FD.
        # A stuck callback retains the original bounded join and is incomplete.
        reader = getattr(self, "_reader", None)
        reader_finished = reader is None
        try:
            if reader and reader is not threading.current_thread():
                reader.join(timeout=1)
                reader_finished = not getattr(reader, "is_alive", lambda: False)()
        except BaseException as error:
            remember(error)
        try:
            if child.stdout:
                child.stdout.close()
        except BaseException as error:
            descriptors_closed = False
            remember(error)
        self._close_cleanup_complete = registry_empty and reaped and reader_finished and descriptors_closed
        if not reaped:
            self.close_cleanup_error = self._failure_text(
                "native child cleanup incomplete: " + getattr(self, "close_cleanup_error", "child not reaped"))
        if original is not None:
            raise original.with_traceback(original_traceback)
