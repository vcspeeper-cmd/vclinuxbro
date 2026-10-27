# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import queue
import ssl
import threading
import time
from email.message import Message
from urllib.parse import urlsplit

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import (
    ConnectionTerminated,
    DataReceived,
    InformationalResponseReceived,
    RemoteSettingsChanged,
    ResponseReceived,
    StreamEnded,
    StreamReset,
    TrailersReceived,
    UnknownFrameReceived,
)
from h2.exceptions import H2Error
from h2.settings import SettingCodes, Settings
from hpack import Encoder
from hpack.hpack import INDEX_INCREMENTAL, INDEX_NEVER
from urllib3._collections import HTTPHeaderDict
from urllib3.exceptions import ProtocolError

from ._http3 import _Call
from ._retry import Http1Required, Rejected, Reset


class FirefoxEncoder(Encoder):
    @Encoder.header_table_size.setter
    def header_table_size(self, value):
        self.header_table.maxsize = value
        self.header_table.resized = True
        minimum = (
            min(value, *self.table_size_changes) if self.table_size_changes else value
        )
        self.table_size_changes = [minimum, value] if minimum < value else [value]

    def add(self, to_add, sensitive, huffman=False):
        name, value = to_add
        never = name == b"authorization" or name == b"cookie" and len(value) < 20
        plain = (
            name == b":path"
            or len(name) + len(value) + 32 > self.header_table_size // 2
            or self.header_table_size < 128
        )
        entries = (*self.header_table.STATIC_TABLE, *self.header_table.dynamic_entries)
        reference = 0
        matched = None
        for index, (entry_name, entry_value) in enumerate(entries, 1):
            if entry_name == name:
                reference = index
                if entry_value == value:
                    matched = index
                    break
        if matched and not never and not plain:
            return self._encode_indexed(matched)
        mode = INDEX_NEVER if never else b"\x00" if plain else INDEX_INCREMENTAL
        encoded = (
            self._encode_indexed_literal(reference, value, mode, True)
            if reference
            else self._encode_literal(name, value, mode, True)
        )
        if not never and not plain:
            self.header_table.add(name, value)
        return encoded


class _Protocol(H2Connection):
    def _receive_goaway_frame(self, frame):
        event = ConnectionTerminated()
        event.error_code = frame.error_code
        event.last_stream_id = frame.last_stream_id
        event.additional_data = frame.additional_data
        return [], [event]


class Connection:
    def __init__(self, transport):
        self.transport = transport
        self.transport.enable_nonblocking()
        self._output = bytearray()
        self._write_buffer = b""
        self._tls_pending = False
        self.protocol = _Protocol(
            H2Configuration(client_side=True, header_encoding=None)
        )
        self.protocol.encoder = FirefoxEncoder()
        self.protocol.local_settings = Settings(
            client=True,
            initial_values={
                SettingCodes.HEADER_TABLE_SIZE: 65536,
                SettingCodes.ENABLE_PUSH: 0,
                SettingCodes.INITIAL_WINDOW_SIZE: 131072,
                SettingCodes.MAX_FRAME_SIZE: 16384,
            },
        )
        # hyper-h2 adds ENABLE_CONNECT_PROTOCOL by default; Firefox omits it.
        del self.protocol.local_settings[SettingCodes.ENABLE_CONNECT_PROTOCOL]
        self.protocol.decoder.max_allowed_table_size = 65536
        self.protocol.initiate_connection()
        self.protocol.increment_flow_control_window(12517377)
        self.streams = {}
        self.closed = False
        self.goaway = False
        self.goaway_error = 0
        self.error = None
        self.received_settings = False
        from ruyijs_necko.coalescing_snapshot import INITIAL
        transport._coalescing_state = INITIAL
        self.commands = queue.Queue()
        from ruyijs_necko.command_budget import Budget
        self._ruyijs_budget = Budget()
        self._command_lock = threading.Lock()
        self.pending = []
        self.wake = threading.Event()
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="necko-http2", daemon=True
        )
        self._thread.start()

    def submit(self, operation, exchange, **arguments):
        from ruyijs_necko.stream_control import timeout, wait_call
        from ruyijs_necko.command_budget import reserve_normal, release_normal
        limit = timeout(exchange, exchange.timeout, "stream-" + operation)
        reserved = not self.closed
        if reserved:
            reserve_normal(self, exchange, arguments)
        try:
            call = _Call(operation, exchange, limit, **arguments)
            with self._command_lock:
                closed = self.closed
                if not closed:
                    self.commands.put(call)
                    reserved = False
                elif reserved:
                    release_normal(self, exchange)
                    reserved = False
        finally:
            if reserved:
                release_normal(self, exchange)
        if closed:
            if exchange.ended and not exchange.error:
                if operation == "read":
                    size = arguments["size"]
                    data = bytes(exchange.buffer[:size])
                    del exchange.buffer[:size]
                    return data
                if operation in ("headers", "finish", "cancel"):
                    return None
            raise (
                exchange.error
                or self.error
                or ProtocolError("HTTP/2 connection closed")
            )
        self.wake.set()
        if getattr(exchange, "_ruyijs_control", None) is not None:
            wait_call(exchange, call, limit)
        elif not call.done.wait(exchange.timeout):
            call.cancelled = True
            self.cancel(exchange)
            raise TimeoutError("HTTP/2 stream timed out")
        if call.error:
            raise call.error
        return call.result

    def cancel(self, exchange):
        from ruyijs_necko.command_budget import cancel as admit_cancel
        admit_cancel(self, exchange, _Call, self.wake.set)

    def flush(self):
        self._output.extend(self.protocol.data_to_send())
        for _ in range(32):
            if not self._write_buffer:
                if not self._output:
                    try:
                        self.transport.write_some(b"")
                        self._tls_pending = False
                    except BlockingIOError:
                        self._tls_pending = True
                    return
                self._write_buffer = bytes(self._output[:16384])
                del self._output[:16384]
            try:
                count = self.transport.write_some(self._write_buffer)
            except BlockingIOError:
                self._tls_pending = True
                return
            if not count:
                return
            self._write_buffer = self._write_buffer[count:]

    def receive(self):
        data = self.transport.read(65536)
        if not data:
            self.closed = True
            raise Reset("HTTP/2 connection ended before stream completion")
        try:
            events = self.protocol.receive_data(data)
            for event in events:
                if isinstance(event, ConnectionTerminated):
                    self.goaway = True
                    self.goaway_error = event.error_code
                    for stream, exchange in self.streams.items():
                        if stream > event.last_stream_id and not exchange.ended:
                            error = (
                                Http1Required if event.error_code == 13 else Rejected
                            )
                            exchange.fail(error("HTTP/2 GOAWAY rejected this request"))
                elif isinstance(event, StreamReset):
                    exchange = self.streams.get(event.stream_id)
                    if exchange is not None:
                        error = (
                            Rejected
                            if event.error_code == 7
                            else Http1Required
                            if event.error_code == 13
                            else ProtocolError
                            if event.error_code in (0, 8)
                            else Reset
                        )
                        exchange.fail(error(f"HTTP/2 stream reset: {event.error_code}"))
                        if event.error_code == 9:
                            self.goaway = True
                elif isinstance(event, RemoteSettingsChanged):
                    self.received_settings = True
                elif (
                    isinstance(event, UnknownFrameReceived) and event.frame.type == 0xC
                ):
                    self.receive_origins(event.frame)
                elif isinstance(
                    event,
                    (
                        ResponseReceived,
                        InformationalResponseReceived,
                        DataReceived,
                        StreamEnded,
                        TrailersReceived,
                    ),
                ):
                    exchange = self.streams.get(event.stream_id)
                    if exchange is None or exchange.closed:
                        if isinstance(event, DataReceived):
                            self.protocol.acknowledge_received_data(
                                event.flow_controlled_length, event.stream_id
                            )
                        continue
                    exchange.received_data = True
                    if isinstance(event, ResponseReceived):
                        exchange.response_headers(event.headers)
                    elif isinstance(event, DataReceived):
                        exchange.buffer.extend(event.data)
                        # Credit is returned only after the caller consumes body data.
                        exchange.credit += event.flow_controlled_length
                    elif isinstance(event, StreamEnded):
                        exchange.ended = True
            self.flush()
        except H2Error as error:
            self.closed = True
            raise ProtocolError(str(error)) from error

    def receive_origins(self, frame):
        if (
            frame.stream_id
            or frame.flag_byte & 0x0F
            or getattr(self.transport, "using_proxy", False)
        ):
            return
        from ruyijs_necko.coalescing_snapshot import origin_frame, add_origin
        origin_frame(self)
        data = frame.body
        while len(data) >= 2:
            size = int.from_bytes(data[:2], "big")
            data = data[2:]
            if size > len(data):
                break
            value, data = data[:size], data[size:]
            try:
                origin = urlsplit(value.decode("ascii"))
                if origin.scheme != "https" or not origin.hostname:
                    continue
                host = origin.hostname.encode("idna").decode("ascii")
                if any(character in host for character in "*\\\x00\r\n\t "):
                    continue
                add_origin(self, host, origin.port or 443)
            except (ValueError, UnicodeError):
                continue
        # The original transport root was committed one bounded origin at a time.

    def idle(self):
        return not self.closed and not self.goaway

    def _execute(self, call):
        exchange = call.exchange
        operation = call.operation
        if call.cancelled:
            raise TimeoutError("HTTP/2 request operation cancelled")
        if operation != "cancel" and exchange.closed:
            raise exchange.error or ProtocolError("HTTP/2 response closed")
        if operation != "cancel" and exchange.error:
            raise exchange.error
        if call.deadline is not None and time.monotonic() >= call.deadline:
            raise TimeoutError("HTTP/2 stream timed out")
        if operation in ("fetch", "write", "finish") and (
            len(self._output) + len(self._write_buffer) >= 65536
        ):
            return False
        if operation == "fetch":
            if self.goaway:
                if self.goaway_error == 13:
                    raise Http1Required("HTTP/2 peer requires HTTP/1.1")
                raise Rejected("HTTP/2 connection is draining")
            maximum = min(100, self.protocol.remote_settings.max_concurrent_streams)
            if self.protocol.open_outbound_streams >= maximum:
                return False
            exchange.stream_id = max(3, self.protocol.get_next_available_stream_id())
            exchange.sent_data = True
            self.protocol.send_headers(
                exchange.stream_id,
                call.arguments["headers"],
                end_stream=exchange.sent_end,
                priority_weight=call.arguments["weight"],
                priority_depends_on=0,
                priority_exclusive=False,
            )
            self.protocol.increment_flow_control_window(
                12582912 - 131072, exchange.stream_id
            )
            self.streams[exchange.stream_id] = exchange
        elif operation == "write":
            window = self.protocol.local_flow_control_window(exchange.stream_id)
            if window <= 0:
                return False
            data = call.arguments["data"]
            count = min(len(data), window, self.protocol.max_outbound_frame_size, 16000)
            self.protocol.send_data(exchange.stream_id, data[:count])
            call.result = count
        elif operation == "finish":
            if not exchange.sent_end:
                self.protocol.end_stream(exchange.stream_id)
                exchange.sent_end = True
        elif operation == "headers":
            if exchange.status is None:
                return False
        elif operation == "read":
            if not exchange.buffer and not exchange.ended:
                return False
            size = call.arguments["size"]
            call.result = bytes(exchange.buffer[:size])
            del exchange.buffer[:size]
            credit = (
                min(len(call.result), exchange.credit)
                if exchange.buffer
                else exchange.credit
            )
            if credit:
                self.protocol.acknowledge_received_data(credit, exchange.stream_id)
                exchange.credit -= credit
        elif operation == "cancel":
            self.streams.pop(exchange.stream_id, None)
            exchange.buffer.clear()
            if exchange.stream_id is not None:
                if exchange.credit:
                    self.protocol.acknowledge_received_data(
                        exchange.credit, exchange.stream_id
                    )
                    exchange.credit = 0
                if not exchange.ended:
                    try:
                        self.protocol.reset_stream(exchange.stream_id, error_code=8)
                    except H2Error:
                        pass
            exchange.fail(ProtocolError("HTTP/2 response closed"))
        return True

    def _run(self):
        try:
            while not self._stopping.is_set():
                self.wake.clear()
                while True:
                    try:
                        self.pending.append(self.commands.get_nowait())
                    except queue.Empty:
                        break
                waiting = []
                for call in self.pending:
                    try:
                        if not self._execute(call):
                            waiting.append(call)
                            continue
                    except Exception as error:
                        call.error = error
                    from ruyijs_necko.command_budget import finish_call
                    finish_call(self, call)
                    call.done.set()
                    self.flush()
                self.pending = waiting
                self.flush()
                for _ in range(32):
                    pending = self.transport.library.necko_tls_pending(
                        self.transport.handle
                    )
                    if pending < 0:
                        raise Reset("HTTP/2 transport closed")
                    if not pending:
                        break
                    try:
                        self.receive()
                    except BlockingIOError:
                        break
                else:
                    continue
                if (
                    self.pending
                    or self._output
                    or self._write_buffer
                    or self._tls_pending
                ):
                    self.wake.wait(0.001)
                else:
                    self.wake.wait(0.01)
            if self._stopping.is_set() and not self.closed:
                self.protocol.close_connection(error_code=0)
                self.flush()
        except Exception as error:
            if isinstance(error, OSError) and not isinstance(
                error, (TimeoutError, ssl.SSLError)
            ):
                error = Reset(str(error))
            self.error = error
        finally:
            with self._command_lock:
                self.closed = True
                self._ruyijs_budget.closed = True
            error = self.error or ProtocolError("HTTP/2 connection closed")
            for exchange in self.streams.values():
                if not exchange.ended:
                    exchange.fail(error)
            while True:
                try:
                    self.pending.append(self.commands.get_nowait())
                except queue.Empty:
                    break
            for call in self.pending:
                call.error = call.exchange.error or error
                from ruyijs_necko.command_budget import finish_call
                finish_call(self, call)
                call.done.set()
            from ruyijs_necko.coalescing_snapshot import finish
            finish(self)
            self.transport._close_native()

    def close(self):
        self._stopping.set()
        self.wake.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1)


class Exchange:
    multiplexed = True

    def __init__(self, transport, method, url, headers, has_body, timeout=None, control=None):
        self.transport = transport
        if not getattr(transport, "h2", None):
            transport.h2 = Connection(transport)
        self.connection = transport.h2
        from ruyijs_necko.command_budget import open_exchange, preview
        try:
            preview(method, url, headers)
            open_exchange(self.connection, self)
        except Exception:
            self.close()
            raise
        self.stream_id = None
        self.timeout = timeout
        self.error = None
        self.received_data = False
        self.sent_data = False
        self.closed = False
        self.buffer = bytearray()
        self.credit = 0
        self.ended = False
        self.sent_end = not has_body
        self.status = None
        self.version = 20
        self.reason = ""
        self.headers = HTTPHeaderDict()
        self.msg = Message()
        target = urlsplit(url)
        path = target.path or "/"
        if target.query:
            path += "?" + target.query
        fields = [
            (b":method", method.encode("ascii")),
            (b":path", path.encode("ascii")),
            (b":authority", headers["Host"].encode("ascii")),
            (b":scheme", target.scheme.encode("ascii")),
        ]
        excluded = {
            "host",
            "connection",
            "proxy-connection",
            "keep-alive",
            "upgrade",
            "transfer-encoding",
        }
        excluded.update(
            item.strip().lower() for item in headers.get("Connection", "").split(",")
        )
        for raw_name, raw_value in headers.items():
            name = raw_name.decode("ascii") if isinstance(raw_name, bytes) else raw_name
            value = (
                raw_value
                if isinstance(raw_value, bytes)
                else raw_value.encode("latin1")
            )
            if name.lower() not in excluded:
                if name.lower() == "cookie":
                    fields.extend((b"cookie", part) for part in value.split(b"; "))
                else:
                    fields.append((name.lower().encode("ascii"), value))
        try:
            from ruyijs_necko.stream_control import attach
            attach(self, control)
            self.connection.submit(
                "fetch",
                self,
                headers=fields,
                weight=42 if headers.get("Sec-Fetch-Dest") == "document" else 22,
            )
        except Exception:
            self.close()
            raise

    def fail(self, error):
        if self.error is None:
            self.error = error

    def response_headers(self, fields):
        for name, value in fields:
            if name == b":status":
                self.status = int(value)
            elif not name.startswith(b":"):
                decoded_name, decoded_value = (
                    name.decode("ascii"),
                    value.decode("latin1"),
                )
                self.headers.add(decoded_name, decoded_value)
                self.msg[decoded_name] = decoded_value

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        view = memoryview(data)
        while view:
            size = self.connection.submit("write", self, data=bytes(view[:16000]))
            view = view[size:]

    def receive(self):
        self.connection.submit("finish", self)
        self.connection.submit("headers", self)

    def read(self, size):
        return self.connection.submit("read", self, size=size)

    def reusable(self):
        return self.ended and not self.buffer

    def close(self):
        from ruyijs_necko.stream_control import close
        close(self)
