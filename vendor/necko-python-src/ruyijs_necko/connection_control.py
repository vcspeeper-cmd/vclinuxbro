"""Explicit DNS and connection controls; original URL/TLS names remain unchanged."""
import socket
import time
from .request_control import RequestControl
from .resolver import Resolver
from .connection_budget import AdmissionError
from .transport_resources import reserve


def resolver(adapter):
    with adapter._lock:
        value = getattr(adapter, "_ruyijs_resolver", None)
        if value is None:
            value = Resolver()
            adapter._ruyijs_resolver = value
        return value


def addresses(adapter, host, port, kind, control, attempt=None):
    if control is None:
        return socket.getaddrinfo(host, port, type=kind)
    child = control.child(attempt.deadline if attempt is not None else None)
    with control.register(lambda: child.cancel("Parent request cancelled")):
        try:
            if attempt is not None:
                attempt.set_interrupt(lambda: child.cancel("Connection attempt cancelled"))
            rows = resolver(adapter).resolve(host, port, kind, child)
        except Exception:
            control.remaining("dns")
            raise
        finally:
            if attempt is not None:
                attempt.detach()
    control.remaining("dns")
    if attempt is not None:
        attempt.remaining()
    return [(row.family, row.kind, row.protocol, "", row.sockaddr) for row in rows]


def numeric_host(address):
    return address[0] + (f"%{address[3]}" if len(address) == 4 and address[3] else "")


def open_socket(adapter, factory, host, port, timeout, attempt, control):
    if control is None:
        return factory(adapter.library, host, port, timeout, attempt)
    rows = addresses(adapter, host, port, socket.SOCK_STREAM, control, attempt)
    error = None
    for _, _, _, _, address in rows:
        control.remaining("tcp-connect")
        if attempt:
            attempt.remaining()
        try:
            token = reserve(adapter, "tcp", control, attempt)
            try:
                options = {"budget_token": token} if token is not None else {}
                return factory(adapter.library, numeric_host(address), port, timeout, attempt, **options)
            finally:
                if token is not None:
                    token.abandon()
        except AdmissionError:
            raise
        except Exception as caught:
            error = caught
            control.remaining("tcp-connect")
    raise error or OSError("No usable TCP address")


def connect(adapter, url, proxy, verify, cert, timeout, http2, control):
    from necko._connect import Attempt
    remaining = control.remaining("connect")
    deadline = min(control.deadline, time.monotonic() + min(remaining, timeout if timeout is not None else remaining))
    attempt = Attempt(deadline)
    registration = control.register(attempt.cancel)
    try:
        connection = adapter._connect(url, proxy, verify, cert, attempt.remaining(),
                                      attempt=attempt, http2=http2, control=control)
        try:
            control.remaining("connected")
        except Exception:
            connection.close()
            raise
        return connection
    except Exception as error:
        # Establishment never carries request bytes; controlled_send reads this
        # as Firefox's !reallySentData when it decides whether to restart.
        error._necko_unsent = True
        raise
    finally:
        # Detach waits for any currently executing Attempt.cancel interrupt.
        attempt.detach()
        registration.close()


def close_resolver(adapter):
    with adapter._lock:
        value = getattr(adapter, "_ruyijs_resolver", None)
        borrowed = getattr(adapter, "_ruyijs_borrowed_resolver", False)
    if value is not None and not borrowed:
        value.close()
