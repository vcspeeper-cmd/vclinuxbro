# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Requests API transport backed by NSS and native HTTP/1 or hyper-h2."""

import ctypes
import io
import math
import os
import re
import socket
import ssl
import sys
import threading
import time
import uuid
import warnings
from collections import OrderedDict
from email.parser import Parser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from cryptography.x509.verification import VerificationError
from requests.adapters import HTTPAdapter as RequestsAdapter
from requests.auth import _basic_auth_str
from requests.exceptions import (
    ConnectionError,
    ConnectTimeout,
    InvalidHeader,
    InvalidProxyURL,
    InvalidSchema,
    ProxyError,
    ReadTimeout,
    RequestException,
    RetryError,
    SSLError,
    UnrewindableBodyError,
)
from requests.structures import CaseInsensitiveDict
from requests.utils import prepend_scheme_if_needed, select_proxy
from urllib3._collections import HTTPHeaderDict
from urllib3.exceptions import (
    ConnectTimeoutError,
    InsecureRequestWarning,
    MaxRetryError,
    NewConnectionError,
    ProtocolError,
    ReadTimeoutError,
)
from urllib3.exceptions import (
    ProxyError as UrllibProxyError,
)
from urllib3.exceptions import (
    SSLError as UrllibSSLError,
)
from urllib3.response import HTTPResponse
from urllib3.util import Timeout

from ._firefox import wire_headers
from ._nss import TLS as _TLS
from ._nss import Context as _ssl_context
from ._nss import configure_library
from ._retry import (
    MAX_REQUEST_ATTEMPTS,
    DirectRouteReset,
    Http1Required,
    Reset,
    restartable,
)

_Pointer = ctypes.c_void_p
_Bytes = ctypes.POINTER(ctypes.c_uint8)
_Read = ctypes.CFUNCTYPE(
    ctypes.c_int, _Pointer, _Bytes, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)
)
_Write = ctypes.CFUNCTYPE(ctypes.c_int, _Pointer, _Bytes, ctypes.c_uint32)
_Cancel = ctypes.CFUNCTYPE(ctypes.c_int, _Pointer)


def _load_library(path=None):
    if path is None:
        path = os.environ.get("NECKO_CORE_LIBRARY")
    if not path:
        name = (
            "necko_core.dll"
            if os.name == "nt"
            else (
                "libnecko_core.dylib"
                if sys.platform == "darwin"
                else "libnecko_core.so"
            )
        )
        candidates = [
            Path(__file__).parent / name,
            Path(__file__).parents[1] / name,
            Path(__file__).parents[3] / "obj-necko" / name,
        ]
        path = next((item for item in candidates if item.is_file()), candidates[0])
    path = Path(path).resolve()
    if os.name == "nt":
        with os.add_dll_directory(str(path.parent)):
            library = ctypes.CDLL(str(path))
    else:
        library = ctypes.CDLL(str(path))
    configure_library(library)
    signatures = {
        "socket_open": (
            [
                ctypes.c_char_p,
                ctypes.c_uint16,
                ctypes.c_uint32,
                ctypes.POINTER(_Pointer),
            ],
            ctypes.c_int,
        ),
        "socket_close": ([_Pointer], None),
        "socket_interrupt": ([_Pointer], None),
        "socket_open_cancellable": (
            [
                ctypes.c_char_p,
                ctypes.c_uint16,
                ctypes.c_uint32,
                _Cancel,
                _Pointer,
                ctypes.POINTER(_Pointer),
            ],
            ctypes.c_int,
        ),
        "socket_idle": ([_Pointer], ctypes.c_int),
        "socket_read": (
            [
                _Pointer,
                _Bytes,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
            ],
            ctypes.c_int,
        ),
        "socket_write": (
            [_Pointer, _Bytes, ctypes.c_uint32, ctypes.c_uint32],
            ctypes.c_int,
        ),
        "exchange_open": (
            [
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
                _Read,
                _Write,
                _Pointer,
                ctypes.POINTER(_Pointer),
            ],
            ctypes.c_int,
        ),
        "exchange_write": ([_Pointer, _Bytes, ctypes.c_uint32], ctypes.c_int),
        "exchange_receive": ([_Pointer], ctypes.c_int),
        "exchange_headers": ([_Pointer], ctypes.c_char_p),
        "exchange_read": (
            [_Pointer, _Bytes, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)],
            ctypes.c_int,
        ),
        "exchange_reusable": ([_Pointer], ctypes.c_int),
        "exchange_close": ([_Pointer], None),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(library, "necko_" + name)
        function.argtypes = arguments
        function.restype = result
    if hasattr(library, "necko_socket_peer"):
        library.necko_socket_peer.argtypes = [
            _Pointer,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint16),
        ]
        library.necko_socket_peer.restype = ctypes.c_int
    library.necko_socket_write_some.argtypes = [
        _Pointer,
        _Bytes,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    library.necko_socket_write_some.restype = ctypes.c_int
    return library


def _milliseconds(value):
    if value is None:
        return 0xFFFFFFFF
    if not math.isfinite(value) or value <= 0 or value * 1000 >= 0xFFFFFFFF:
        raise ValueError("timeout must be positive, finite, and below 4294967 seconds")
    return max(1, math.ceil(value * 1000))


class _NativeSocket:
    def __init__(self, library, host, port, timeout, attempt=None, budget_token=None):
        from ruyijs_necko.connection_lifetime import native_initialize
        native_initialize(self, self._initialize,
                          (library, host, port, timeout, attempt), budget_token)

    def _initialize(self, library, host, port, timeout, attempt):
        self.library = library
        self.handle = _Pointer()
        self._handle_lock = threading.Lock()
        self.attempt = attempt
        self.timeout = timeout
        if attempt is None:
            result = library.necko_socket_open(
                host.encode("idna"),
                port,
                _milliseconds(timeout),
                ctypes.byref(self.handle),
            )
        else:
            cancelled = _Cancel(lambda _: int(attempt.cancelled.is_set()))
            result = library.necko_socket_open_cancellable(
                host.encode("idna"),
                port,
                _milliseconds(attempt.remaining()),
                cancelled,
                None,
                ctypes.byref(self.handle),
            )
        if result == 5:
            raise ConnectTimeout(f"Connection to {host}:{port} timed out")
        if result:
            error = ConnectionError(
                f"Native connection error {result} for {host}:{port}"
            )
            error._necko_connect = True
            raise error
        if attempt is not None:
            attempt.set_interrupt(self.interrupt)
        self.peer_address = None
        self.peer_port = None
        if hasattr(library, "necko_socket_peer"):
            host_buffer = ctypes.create_string_buffer(128)
            peer_port = ctypes.c_uint16()
            if not library.necko_socket_peer(
                self.handle, host_buffer, 128, ctypes.byref(peer_port)
            ):
                self.peer_address = host_buffer.value.decode("ascii")
                self.peer_port = peer_port.value

    def idle(self):
        return bool(self.handle and self.library.necko_socket_idle(self.handle))

    def read(self, size):
        if self.attempt is not None:
            from ruyijs_necko.socket_control import read
            return read(self, size)
        buffer = (ctypes.c_uint8 * size)()
        length = ctypes.c_uint32()
        result = self.library.necko_socket_read(
            self.handle,
            buffer,
            size,
            _milliseconds(self._timeout()),
            ctypes.byref(length),
        )
        if result == 5:
            raise TimeoutError("Read timed out")
        if result:
            raise OSError(f"Native socket read error {result}")
        return bytes(buffer[: length.value])

    def write(self, data):
        if self.attempt is not None:
            from ruyijs_necko.socket_control import write
            return write(self, data)
        buffer = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        result = self.library.necko_socket_write(
            self.handle, buffer, len(data), _milliseconds(self._timeout())
        )
        if result == 5:
            raise TimeoutError("Write timed out")
        if result:
            raise OSError(f"Native socket write error {result}")

    def read_some(self, size):
        buffer = (ctypes.c_uint8 * size)()
        length = ctypes.c_uint32()
        result = self.library.necko_socket_read(
            self.handle, buffer, size, 0, ctypes.byref(length)
        )
        if result == 5:
            raise BlockingIOError("Socket read would block")
        if result:
            raise OSError(f"Native socket read error {result}")
        return bytes(buffer[: length.value])

    def write_some(self, data):
        buffer = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        length = ctypes.c_uint32()
        result = self.library.necko_socket_write_some(
            self.handle, buffer, len(data), ctypes.byref(length)
        )
        if result == 5:
            raise BlockingIOError("Socket write would block")
        if result:
            raise OSError(f"Native socket write error {result}")
        return length.value

    def _timeout(self):
        return self.attempt.remaining() if self.attempt is not None else self.timeout

    def interrupt(self):
        with self._handle_lock:
            if self.handle:
                self.library.necko_socket_interrupt(self.handle)

    def close(self):
        from ruyijs_necko.connection_lifetime import native_closed
        token = getattr(self, "_ruyijs_token", None)
        if token is not None:
            token.closing()
        with self._handle_lock:
            if self.handle:
                self.library.necko_socket_close(self.handle)
                self.handle = _Pointer()
            native_closed(self)

    def __del__(self):
        if getattr(self, "_handle_lock", None) is not None:
            self.close()


class _Pool:
    def __init__(self, maximum, block):
        self.maximum = maximum
        self.block = block
        self.idle = []
        self.shared = {}
        self.connecting = False
        self.leased = 0
        self.closed = False
        self.condition = threading.Condition()

    def acquire(self, prefer=None, http3=True, control=None):
        while True:
            remaining = control.remaining("pool") if control is not None else None
            with self.condition:
                if self.closed:
                    raise ConnectionError("Connection pool closed")
                for connection in list(self.shared):
                    if (
                        connection.idle()
                        and (prefer is None or connection.alpn == prefer)
                        and (http3 or connection.alpn != "h3")
                    ):
                        self.shared[connection] += 1
                        self.leased += 1
                        return connection
                    if self.shared[connection] == 0 and not connection.idle():
                        self.shared.pop(connection)
                        connection.close()
                if self.connecting or self.block and self.leased >= self.maximum:
                    self.condition.wait(min(remaining, .025) if control is not None else None)
                    continue
                self.leased += 1
                for index in range(len(self.idle) - 1, -1, -1):
                    connection = self.idle[index]
                    if not connection.idle():
                        self.idle.pop(index).close()
                    elif prefer is None or getattr(connection, "alpn", None) == prefer:
                        return self.idle.pop(index)
                self.connecting = True
                return None

    def register(self, connection):
        with self.condition:
            if getattr(connection, "multiplexed", False):
                self.shared[connection] = self.shared.get(connection, 0) + 1
            self.connecting = False
            self.condition.notify_all()

    def borrow(self, host, port, addresses, prefer=None, http3=True):
        with self.condition:
            if self.closed:
                return None
            for connection in self.shared:
                if not connection.idle() or prefer and connection.alpn != prefer:
                    continue
                if not http3 and connection.alpn == "h3":
                    continue
                if getattr(connection, "origin_port", None) != port:
                    continue
                from ruyijs_necko.coalescing_snapshot import allowed
                permitted, origin_allowed = allowed(connection, host, port)
                if not permitted:
                    continue
                protocol = getattr(connection, "h2", None)
                if protocol is not None and not protocol.received_settings:
                    continue
                if not origin_allowed and (
                    addresses is None
                    or getattr(connection, "peer_address", None) not in addresses
                ):
                    continue
                try:
                    connection.context.verify(host, connection.certificate_chain)
                except (AttributeError, ValueError, ssl.SSLError, VerificationError):
                    continue
                self.shared[connection] += 1
                self.leased += 1
                return connection
        return None

    def release(self, connection):
        with self.condition:
            self.leased -= 1
            if connection in self.shared:
                self.shared[connection] -= 1
                if self.shared[connection] == 0 and (
                    self.closed or not connection.idle()
                ):
                    self.shared.pop(connection)
                    connection.close()
                self.condition.notify_all()
                return
            if connection is None:
                self.connecting = False
            if connection is not None:
                if self.closed or len(self.idle) >= self.maximum:
                    connection.close()
                else:
                    self.idle.append(connection)
            self.condition.notify()

    def close(self):
        with self.condition:
            self.closed = True
            for connection in self.idle:
                connection.close()
            for connection, users in list(self.shared.items()):
                if not users:
                    connection.close()
                    self.shared.pop(connection)
            self.idle.clear()
            self.condition.notify_all()


class _Exchange:
    def __init__(self, library, transport, method, target, headers, control=None):
        self.library = library
        self.transport = transport
        self.handle = _Pointer()
        self.error = None
        self.sent_data = False
        self.received_data = False
        self._read_callback = _Read(self._read)
        self._write_callback = _Write(self._write)
        wire = "".join(
            f"{name.decode('ascii') if isinstance(name, bytes) else name}: "
            f"{value.decode('latin1') if isinstance(value, bytes) else value}\r\n"
            for name, value in headers.items()
        )
        if "\x00" in wire or "\x00" in method or "\x00" in target:
            raise InvalidHeader("NUL in request")

        self._ruyijs_control = control
        self._ruyijs_h1 = None
        try:
            if control is not None:
                from ruyijs_necko.stream_control import H1Control
                self._ruyijs_h1 = H1Control(self, control)
            result = library.necko_exchange_open(
                method.encode("ascii"), target.encode("ascii"), wire.encode("latin1"),
                self._read_callback, self._write_callback, None, ctypes.byref(self.handle),
            )
            self.check(result)
        except Exception:
            self.close()
            raise

    def _read(self, _closure, buffer, capacity, length):
        try:
            data = self.transport.read(capacity)
            self.received_data |= bool(data)
            if not data and not self.received_data:
                raise Reset("HTTP/1 connection ended before a response")
            ctypes.memmove(buffer, data, len(data))
            length[0] = len(data)
            return 0
        except Exception as error:
            self.error = error
            return 6

    def _write(self, _closure, buffer, length):
        try:
            self.sent_data |= bool(length)
            self.transport.write(ctypes.string_at(buffer, length))
            return 0
        except Exception as error:
            self.error = error
            return 6

    def check(self, result):
        if self.error is not None:
            error, self.error = self.error, None
            if isinstance(error, OSError) and not isinstance(
                error, (TimeoutError, ssl.SSLError)
            ):
                error = Reset(str(error))
            error._necko_sent = self.sent_data
            error._necko_received = self.received_data
            raise error
        if result == 1:
            raise InvalidHeader("Invalid native request framing or Content-Length")
        if result:
            raise ProtocolError(f"Native HTTP framing error {result}")

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        view = memoryview(data)
        for start in range(0, len(view), 65536):
            part = view[start : start + 65536]
            buffer = (ctypes.c_uint8 * len(part)).from_buffer_copy(part)
            self.check(
                self.library.necko_exchange_write(self.handle, buffer, len(part))
            )

    def receive(self):
        self.check(self.library.necko_exchange_receive(self.handle))
        wire = self.library.necko_exchange_headers(self.handle).decode("latin1")
        status, _, fields = wire.partition("\r\n")
        version, code, *reason = status.split(" ", 2)
        self.version = 11 if version == "HTTP/1.1" else 10
        self.status = int(code)
        self.reason = reason[0] if reason else ""
        self.msg = Parser().parsestr(fields)
        self.headers = HTTPHeaderDict()
        for name, value in self.msg.items():
            self.headers.add(name, value)

    def read(self, size):
        buffer = (ctypes.c_uint8 * size)()
        length = ctypes.c_uint32()
        self.check(
            self.library.necko_exchange_read(
                self.handle, buffer, size, ctypes.byref(length)
            )
        )
        return bytes(buffer[: length.value])

    def close(self):
        guard = getattr(self, "_ruyijs_h1", None)
        if guard is not None:
            guard.close()
        if self.handle:
            self.library.necko_exchange_close(self.handle)
            self.handle = _Pointer()
        self._read_callback = self._write_callback = None

    def reusable(self):
        return bool(self.library.necko_exchange_reusable(self.handle))


class _Body(io.RawIOBase):
    def __init__(self, exchange, pool):
        super().__init__()
        self.exchange = exchange
        self.pool = pool
        self.msg = exchange.msg

    def isclosed(self):
        return self.closed

    def read(self, size=-1):
        if self.closed or size == 0:
            return b""
        pieces = []
        remaining = size
        try:
            while remaining != 0:
                from ruyijs_necko.stream_control import check
                check(self.exchange, "body-read")
                chunk = self.exchange.read(
                    65536
                    if remaining is None or remaining < 0
                    else min(remaining, 65536)
                )
                check(self.exchange, "body-read-complete")
                if not chunk:
                    self.close()
                    break
                pieces.append(chunk)
                if remaining is not None and remaining > 0:
                    remaining -= len(chunk)
                if self.exchange.reusable():
                    self.close()
                    break
            return b"".join(pieces)
        except Exception:
            self.close(reuse=False)
            from ruyijs_necko.stream_control import check
            check(self.exchange, "body-error")
            raise

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def read1(self, size=-1):
        if self.closed or size == 0:
            return b""
        try:
            from ruyijs_necko.stream_control import check
            check(self.exchange, "body-read")
            data = self.exchange.read(
                65536 if size is None or size < 0 else min(size, 65536)
            )
            check(self.exchange, "body-read-complete")
            if not data or self.exchange.reusable():
                self.close()
            return data
        except Exception:
            self.close(reuse=False)
            from ruyijs_necko.stream_control import check
            check(self.exchange, "body-error")
            raise

    def close(self, reuse=True):
        if not self.closed:
            exchange = self.exchange
            connection = exchange.transport
            reusable = reuse and exchange.reusable()
            exchange.close()
            if getattr(exchange, "multiplexed", False):
                self.pool.release(connection)
                super().close()
                return
            if not reusable:
                connection.close()
            self.pool.release(connection if reusable else None)
            super().close()


class HeaderMap(CaseInsensitiveDict):
    def __init__(self, items=(), **kwargs):
        self._values = HTTPHeaderDict()
        super().__init__()
        for name, value in items.items() if hasattr(items, "items") else items:
            self._values.add(name, value)
            super().__setitem__(name, self._values[name])
        self.update(kwargs)

    def __setitem__(self, key, value):
        self._values[key] = value
        super().__setitem__(key, value)

    def __delitem__(self, key):
        del self._values[key]
        super().__delitem__(key)

    def get_all(self, name, default=None):
        return self._values.getlist(name) or ([] if default is None else default)

    def getlist(self, name):
        return self.get_all(name)


class HTTPAdapter(RequestsAdapter):
    __attrs__ = RequestsAdapter.__attrs__ + ["library_path"]

    def __init__(
        self,
        pool_connections=10,
        pool_maxsize=10,
        max_retries=0,
        pool_block=False,
        library_path=None,
    ):
        if pool_connections < 1 or pool_maxsize < 1:
            raise ValueError("pool sizes must be positive")
        self.library_path = library_path
        self._library = None
        self._pools = OrderedDict()
        self._lock = threading.Lock()
        self._tls_partition = uuid.uuid4().hex
        from ruyijs_necko.cache_snapshot import EMPTY
        self._alt_svc = EMPTY
        self._quic_tokens = EMPTY
        self._token_lock = threading.Lock()
        super().__init__(pool_connections, pool_maxsize, max_retries, pool_block)

    def __setstate__(self, state):
        self._tls_partition = uuid.uuid4().hex
        from ruyijs_necko.cache_snapshot import EMPTY
        self._alt_svc = EMPTY
        self._quic_tokens = EMPTY
        self._token_lock = threading.Lock()
        self._library = None
        self._pools = OrderedDict()
        self._lock = threading.Lock()
        super().__setstate__(state)

    @property
    def library(self):
        with self._lock:
            if self._library is None:
                self._library = _load_library(self.library_path)
            return self._library

    def _pool(self, key):
        with self._lock:
            if key not in self._pools:
                self._pools[key] = _Pool(self._pool_maxsize, self._pool_block)
            self._pools.move_to_end(key)
            pool = self._pools[key]
            while len(self._pools) > self._pool_connections:
                self._pools.popitem(last=False)[1].close()
            return pool

    def _connect(self, url, proxy, verify, cert, timeout, attempt=None, http2=True, control=None):
        if control is not None and attempt is None:
            from ruyijs_necko.connection_control import connect
            return connect(self, url, proxy, verify, cert, timeout, http2, control)
        target = urlsplit(url)
        endpoint = urlsplit(proxy) if proxy else target
        socks = endpoint.scheme in ("socks4", "socks4a", "socks5", "socks5h")
        if endpoint.scheme not in ("http", "https") and not socks:
            raise InvalidSchema(f"Unsupported proxy scheme: {endpoint.scheme}")
        from ruyijs_necko.connection_control import open_socket
        connection = open_socket(
            self, _NativeSocket,
            endpoint.hostname,
            endpoint.port
            or (1080 if socks else 443 if endpoint.scheme == "https" else 80),
            timeout,
            attempt,
            control,
        )
        socket_transport = connection
        try:
            if socks:
                from ._socks import negotiate

                negotiate(connection, endpoint, target)
            if proxy and endpoint.scheme == "https":
                connection = _TLS(
                    connection,
                    _ssl_context(verify, None, Path(self.library._name).parent),
                    endpoint.hostname,
                    self.library,
                    self._tls_partition + ":proxy:" + endpoint.netloc,
                    http2=False,
                )
            if proxy and not socks and target.scheme == "https":
                host = (
                    f"[{target.hostname}]"
                    if ":" in target.hostname
                    else target.hostname
                )
                authority = f"{host}:{target.port or 443}"
                headers = {"Host": authority}
                if endpoint.username is not None:
                    headers["Proxy-Authorization"] = _basic_auth_str(
                        unquote(endpoint.username), unquote(endpoint.password or "")
                    )
                tunnel = _Exchange(
                    self.library, connection, "CONNECT", authority, headers
                )
                try:
                    tunnel.receive()
                    if tunnel.status != 200:
                        raise ProxyError(f"Proxy CONNECT returned {tunnel.status}")
                finally:
                    tunnel.close()
            if target.scheme == "https":
                connection = _TLS(
                    connection,
                    _ssl_context(verify, cert, Path(self.library._name).parent),
                    target.hostname,
                    self.library,
                    self._tls_partition + ":origin:" + target.netloc + ":" + str(proxy),
                    http2=http2,
                )
                if connection.alpn == "h2":
                    from ._http2 import Connection as H2Connection

                    connection.multiplexed = True
                    connection.using_proxy = bool(proxy)
                    connection.h2 = H2Connection(connection)
                connection.origin_port = target.port or 443
                connection.peer_address = getattr(
                    connection.transport, "peer_address", None
                )
            socket_transport.attempt = None
            return connection
        except TimeoutError as error:
            connection.close()
            raise ConnectTimeout(error) from error
        except Exception:
            connection.close()
            raise

    def _connect_alternative(self, request, key, alternate, verify, timeout, control=None):
        from ._connect import race
        from ._http3 import Connection as H3Connection

        target = urlsplit(request.url)

        def quic(attempt):
            last_error = None
            from ruyijs_necko.connection_control import addresses
            for resolved in addresses(self, *alternate[0], socket.SOCK_DGRAM, control, attempt):
                address = resolved[4]
                from ruyijs_necko.transport_resources import reserve
                budget_token = reserve(self, "h3", control, attempt)
                try:
                    connection = H3Connection(
                        target.hostname,
                        target.port or 443,
                        _ssl_context(verify, None, Path(self.library._name).parent),
                        self.library._name,
                        attempt.remaining(),
                        address[:2],
                        self._quic_token(key),
                        lambda token, age: self._save_quic_token(key, token, age),
                        attempt=attempt,
                        resolved=resolved if control is not None else None,
                        budget_token=budget_token,
                    )
                    connection.origin_port = target.port or 443
                    connection.alternate_endpoint = alternate[0]
                    return connection
                except (OSError, ProtocolError, TimeoutError) as error:
                    self._save_quic_token(key, "", 0)
                    last_error = error
                    if attempt.cancelled.is_set():
                        raise
                finally:
                    if budget_token is not None:
                        budget_token.abandon()
            raise last_error or OSError("No HTTP/3 endpoint addresses")

        connection = race(
            quic,
            lambda attempt: self._connect(
                request.url, None, verify, None, attempt.remaining(), attempt, control=control
            ),
            timeout,
            control=control,
        )
        if connection.alpn != "h3":
            from ruyijs_necko.cache_snapshot import drop
            drop(self, "_alt_svc", key, expected=alternate)
        return connection

    def send(
        self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None,
        control=None,
    ):
        from ruyijs_necko.transport_resources import check_request
        check_request(self, control)
        if control is not None:
            from ruyijs_necko.controlled_send import send
            return send(self, request, stream, timeout, verify, cert, proxies, control)
        retries = self.max_retries
        body_position = None
        if hasattr(request.body, "tell"):
            try:
                body_position = request.body.tell()
            except OSError:
                body_position = None
        attempted = False
        protocol_retries = 0
        http1 = False
        http3 = True
        while True:
            if (
                attempted
                and request.body is not None
                and not isinstance(request.body, (str, bytes, bytearray, memoryview))
            ):
                if body_position is None or not hasattr(request.body, "seek"):
                    raise UnrewindableBodyError(
                        "Request body cannot be replayed for retry", request=request
                    )
                try:
                    request.body.seek(body_position)
                except (OSError, ValueError) as error:
                    raise UnrewindableBodyError(
                        "Request body could not be rewound for retry", request=request
                    ) from error
            attempted = True
            try:
                response = self._send_once(
                    request, stream, timeout, verify, cert, proxies, http1, http3
                )
            except (ConnectionError, ReadTimeout) as error:
                http1 |= getattr(error, "_necko_http1", False)
                http3 &= not getattr(error, "_necko_direct", False)
                if (
                    getattr(error, "_necko_restartable", False)
                    and protocol_retries < MAX_REQUEST_ATTEMPTS - 1
                ):
                    protocol_retries += 1
                    continue
                if isinstance(error, SSLError):
                    cause = UrllibSSLError(str(error))
                elif isinstance(error, ProxyError):
                    cause = UrllibProxyError(
                        str(error), NewConnectionError(None, str(error))
                    )
                elif isinstance(error, ConnectTimeout):
                    cause = ConnectTimeoutError(str(error))
                elif getattr(error, "_necko_connect", False):
                    cause = NewConnectionError(None, str(error))
                elif isinstance(error, ReadTimeout):
                    cause = ReadTimeoutError(None, request.url, str(error))
                else:
                    cause = ProtocolError(str(error))
                try:
                    retries = retries.increment(
                        request.method, request.url, error=cause
                    )
                except (
                    MaxRetryError,
                    ProtocolError,
                    ReadTimeoutError,
                    ConnectTimeoutError,
                    UrllibSSLError,
                    UrllibProxyError,
                ):
                    raise error
                retries.sleep()
                continue
            response.raw.retries = retries
            if (
                response.status_code == 421
                and getattr(response, "_necko_coalesced", False)
                and protocol_retries < MAX_REQUEST_ATTEMPTS - 1
            ):
                protocol_retries += 1
                transport = response._necko_transport
                from ruyijs_necko.coalescing_snapshot import deny
                deny(transport, urlsplit(request.url).hostname)
                response.close()
                continue
            if not retries.is_retry(
                request.method, response.status_code, "Retry-After" in response.headers
            ):
                return response
            try:
                next_retries = retries.increment(
                    request.method, request.url, response=response.raw
                )
            except MaxRetryError as error:
                if not retries.raise_on_status:
                    return response
                response.close()
                raise RetryError(error, request=request) from error
            try:
                response.content
            finally:
                response.close()
            retries = next_retries
            retries.sleep(response.raw)

    def _send_once(
        self, request, stream, timeout, verify, cert, proxies, http1=False, http3=True,
        control=None,
    ):
        if isinstance(timeout, tuple):
            if len(timeout) != 2:
                raise ValueError("timeout must contain (connect, read)")
            timeouts = Timeout(connect=timeout[0], read=timeout[1])
        elif isinstance(timeout, Timeout):
            timeouts = timeout.clone()
        else:
            timeouts = Timeout(connect=timeout, read=timeout)
        connect_timeout = Timeout.resolve_default_timeout(timeouts.connect_timeout)
        timeouts.start_connect()
        read_timeout = timeouts.read_timeout
        _milliseconds(connect_timeout)
        _milliseconds(read_timeout)
        target = urlsplit(request.url)
        proxy = select_proxy(request.url, proxies or {})
        if proxy:
            proxy = prepend_scheme_if_needed(proxy, "http")
            if not urlsplit(proxy).hostname:
                raise InvalidProxyURL(f"Invalid proxy URL: {proxy}")
        if not verify and (
            target.scheme == "https" or proxy and proxy.startswith("https:")
        ):
            warnings.warn(
                f"Unverified HTTPS request to {target.hostname}",
                InsecureRequestWarning,
                stacklevel=2,
            )
        key = (
            target.scheme,
            target.hostname,
            target.port,
            proxy,
            str(verify),
            str(cert),
            http1,
        )
        pool = self._pool(key)
        alternate = self._alternative(key)
        if not http3 or http1 or alternate and alternate[1] <= time.monotonic():
            alternate = None
        transport = pool.acquire("h3" if alternate else None, http3=http3, control=control)
        if control is not None:
            from ruyijs_necko.lease import Lease
            pool = Lease(pool)
        reused = transport is not None
        coalesced = False
        if (
            transport is None
            and target.scheme == "https"
            and verify
            and not proxy
            and not cert
            and not http1
        ):
            try:
                with self._lock:
                    candidates = [
                        (candidate_key, candidate)
                        for candidate_key, candidate in self._pools.items()
                        if candidate is not getattr(pool, "pool", pool)
                        and candidate_key[0] == "https"
                        and candidate_key[3:] == key[3:]
                    ]
                for resolve in (False, True) if candidates else ():
                    addresses = None
                    if resolve:
                        from ruyijs_necko.connection_control import addresses as resolve_addresses
                        addresses = {
                            address[4][0] for address in resolve_addresses(
                                self, target.hostname, target.port or 443,
                                socket.SOCK_STREAM, control,
                            )
                        }
                    for _, candidate in candidates:
                        transport = candidate.borrow(
                            target.hostname,
                            target.port or 443,
                            addresses,
                            "h3" if alternate else None,
                            http3=http3,
                        )
                        if transport is not None:
                            if control is not None:
                                pool = pool.transfer(candidate)
                            else:
                                pool.release(None)
                                pool = candidate
                            coalesced = True
                            reused = True
                            break
                    if coalesced:
                        break
            except socket.gaierror:
                pass
            except Exception:
                pool.release(None)
                raise
        exchange = None
        native_body = None
        try:
            if control is not None:
                control.remaining("connect")
            if transport is None:
                try:
                    if (
                        alternate
                        and not proxy
                        and not cert
                        and target.scheme == "https"
                        and not http1
                    ):
                        transport = self._connect_alternative(
                            request, key, alternate, verify, connect_timeout, control=control
                        )
                    if transport is None:
                        transport = self._connect(
                            request.url,
                            proxy,
                            verify,
                            cert,
                            connect_timeout,
                            http2=not http1,
                            control=control,
                        )
                    pool.register(transport)
                except ConnectionError as error:
                    if proxy and not isinstance(error, (SSLError, ProxyError)):
                        raise ProxyError(error, request=request) from error
                    raise
            read_timeout = timeouts.read_timeout
            if read_timeout is not None and read_timeout <= 0:
                raise ReadTimeout(
                    "Total timeout elapsed during connection", request=request
                )
            if not getattr(transport, "multiplexed", False):
                transport.timeout = read_timeout
            self.add_headers(
                request,
                stream=stream,
                timeout=timeout,
                verify=verify,
                cert=cert,
                proxies=proxies,
            )
            headers = request.headers.copy()
            if "Host" not in headers:
                host = (
                    f"[{target.hostname}]"
                    if ":" in target.hostname
                    else target.hostname
                )
                if target.port and target.port != (
                    443 if target.scheme == "https" else 80
                ):
                    host += f":{target.port}"
                headers["Host"] = host
            if request.body is not None and "Content-Length" not in headers:
                headers["Transfer-Encoding"] = "chunked"
            if proxy and not proxy.startswith("socks") and target.scheme == "http":
                endpoint = urlsplit(proxy)
                if endpoint.username is not None:
                    headers["Proxy-Authorization"] = _basic_auth_str(
                        unquote(endpoint.username), unquote(endpoint.password or "")
                    )
            elif "Proxy-Authorization" in headers:
                del headers["Proxy-Authorization"]
            protocol = getattr(transport, "alpn", None)
            endpoint = getattr(transport, "alternate_endpoint", None)
            if protocol == "h3" and endpoint is not None:
                host, port = endpoint
                host = f"[{host}]" if ":" in host and not host.startswith("[") else host
                headers["Alt-Used"] = host if port == 443 else f"{host}:{port}"
            headers = wire_headers(headers, protocol == "h2", request.method)
            if control is not None:
                from ruyijs_necko.upload import headers as check_headers
                check_headers(headers)
            if protocol in ("h2", "h3"):
                if protocol == "h3":
                    from ._http3 import Exchange
                else:
                    from ._http2 import Exchange

                exchange = Exchange(
                    transport,
                    request.method,
                    request.url,
                    headers,
                    request.body is not None,
                    read_timeout,
                    control=control,
                )
            else:
                exchange = _Exchange(
                    self.library,
                    transport,
                    request.method,
                    self.request_url(request, proxies),
                    headers,
                    control=control,
                )
            body = request.body
            if control is not None:
                control.remaining("upload")
            if control is not None:
                from ruyijs_necko.upload import write
                from ruyijs_necko.wire_fields import WireFields
                expected = headers.get("Content-Length") if isinstance(headers, WireFields) else None
                write(exchange, body, control, int(expected) if expected is not None else None)
            elif isinstance(body, (str, bytes, bytearray, memoryview)):
                exchange.write(body)
            elif hasattr(body, "read"):
                while True:
                    part = body.read(65536)
                    if not part:
                        break
                    exchange.write(part)
            elif body is not None:
                for part in body:
                    exchange.write(part)
            if control is not None:
                control.remaining("headers")
            exchange.receive()
            if control is not None:
                control.remaining("headers-complete")
            if target.scheme == "https" and not proxy and not cert:
                self._remember_alternative(key, target.hostname, exchange.headers)
            native_body = _Body(exchange, pool)
            raw = HTTPResponse(
                body=native_body,
                headers=exchange.headers,
                status=exchange.status,
                version=exchange.version,
                reason=exchange.reason,
                preload_content=False,
                decode_content=False,
                original_response=native_body,
                request_method=request.method,
                request_url=request.url,
            )
            response = self.build_response(request, raw)
            response.headers = HeaderMap(exchange.headers.items())
            response.tls = getattr(transport, "info", None)
            response._necko_coalesced = coalesced
            response._necko_transport = transport
            return response
        except Exception as error:
            should_restart = restartable(error, request.method, exchange, reused)
            if control is not None:
                from ruyijs_necko.controlled_send import replay_allowed
                should_restart = should_restart and replay_allowed(request.method, exchange, error)
            from ruyijs_necko.lease import release_failed
            release_failed(exchange, transport, pool, native_body)
            if control is not None:
                control.remaining("failure")
            if isinstance(error, ssl.SSLError):
                raise SSLError(error, request=request) from error
            if isinstance(error, TimeoutError):
                raise ReadTimeout(error, request=request) from error
            if isinstance(error, ConnectionError):
                error.request = request
            if isinstance(error, ProtocolError):
                converted = ConnectionError(error, request=request)
                converted._necko_restartable = should_restart
                converted._necko_http1 = isinstance(error, Http1Required)
                converted._necko_direct = isinstance(error, DirectRouteReset)
                raise converted from error
            if isinstance(error, OSError) and not isinstance(error, RequestException):
                if transport is not None:
                    raise ConnectionError(error, request=request) from error
            raise

    def _alternative(self, key):
        from ruyijs_necko.cache_snapshot import lookup
        return lookup(self, "_alt_svc", key)

    def _clear_alternatives(self):
        from ruyijs_necko.cache_snapshot import clear
        clear(self, "_alt_svc")

    def _quic_token(self, key):
        from ruyijs_necko.cache_snapshot import lookup
        result = lookup(self, "_quic_tokens", key)
        return result[0] if result is not None else ""

    def _save_quic_token(self, key, token, age):
        from ruyijs_necko.cache_snapshot import put
        put(self, "_quic_tokens", key, token, age)

    def _remember_alternative(self, key, host, headers):
        value = headers.get("Alt-Svc", "")
        if value.strip() == "clear":
            from ruyijs_necko.cache_snapshot import drop
            drop(self, "_alt_svc", key)
            return
        for entry in re.split(r",(?=\s*[a-zA-Z0-9-]+=)", value):
            match = re.fullmatch(r'\s*h3="([^"\\]+)"\s*((?:;[^,]*)?)', entry)
            if not match:
                continue
            authority, parameters = match.groups()
            try:
                endpoint = urlsplit(
                    "https://"
                    + (host + authority if authority.startswith(":") else authority)
                )
                port = endpoint.port
                if (
                    not endpoint.hostname
                    or not port
                    or endpoint.username
                    or endpoint.path
                ):
                    continue
                age_match = re.search(r";\s*ma\s*=\s*(\d+)", parameters)
                age = int(age_match.group(1)) if age_match else 86400
            except ValueError:
                continue
            from ruyijs_necko.cache_snapshot import put
            put(self, "_alt_svc", key, (endpoint.hostname, port), min(age, 86400))
            return

    def close(self):
        from ruyijs_necko.connection_control import close_resolver
        close_resolver(self)
        from ruyijs_necko.cache_snapshot import clear
        clear(self, "_alt_svc", closing=True)
        clear(self, "_quic_tokens", closing=True)
        with self._lock:
            for pool in self._pools.values():
                pool.close()
            self._pools.clear()
        super().close()
