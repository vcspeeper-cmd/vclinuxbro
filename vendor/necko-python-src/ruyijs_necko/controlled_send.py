"""Controlled orchestration of the existing adapter, without a protocol clone."""
from urllib.parse import urlsplit
from requests.exceptions import ConnectionError, ReadTimeout, UnrewindableBodyError
from urllib3.util import Timeout


def bounded_timeout(timeout, control):
    remaining = control.remaining("attempt")
    if isinstance(timeout, tuple):
        if len(timeout) != 2:
            raise ValueError("timeout must contain connect and read")
        connect, read = timeout
    elif isinstance(timeout, Timeout):
        connect = timeout.connect_timeout
        clone = timeout.clone()
        clone.start_connect()
        read = clone.read_timeout
    else:
        connect = read = timeout
    def bounded(value):
        value = Timeout.resolve_default_timeout(value)
        return remaining if value is None else min(remaining, value)
    return Timeout(total=remaining, connect=bounded(connect), read=bounded(read))


def replay_allowed(method, exchange, error):
    from necko._retry import Rejected
    received = getattr(exchange, "received_data", getattr(error, "_necko_received", False))
    if received:
        return False
    if isinstance(error, Rejected):
        return True
    sent = getattr(exchange, "sent_data", getattr(error, "_necko_sent", True))
    return method.upper() in {"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"} or not sent


# NSPR codes Firefox maps to NS_ERROR_NET_RESET (nsSocketTransport2.cpp).
RESET_CODES = {"PR_END_OF_FILE_ERROR", "PR_CONNECT_RESET_ERROR", "PR_CONNECT_ABORTED_ERROR"}


def unsent_reset(error):
    """A reset or EOF while the connection was being established. Firefox
    restarts such a transaction (nsHttpTransaction::Close, !reallySentData)."""
    from necko._retry import Reset
    unsent = reset = False
    pending, seen = [error], 0
    while pending and seen < 16:
        error, seen = pending.pop(), seen + 1
        unsent |= getattr(error, "_necko_unsent", False)
        reset |= isinstance(error, (Reset, ConnectionResetError, ConnectionAbortedError))
        for item in (error.__cause__, error.__context__, *getattr(error, "args", ())):
            if isinstance(item, BaseException):
                pending.append(item)
            elif isinstance(item, (str, tuple)):
                reset |= any(code in str(item) for code in RESET_CODES)
    return unsent and reset


def send(adapter, request, stream, timeout, verify, cert, proxies, control):
    from .upload import validate
    validate(request, control)
    body_position = None
    if hasattr(request.body, "tell"):
        try:
            body_position = request.body.tell()
        except OSError:
            pass
    attempted, http1, http3 = False, False, True
    while True:
        control.begin_attempt()
        if attempted and request.body is not None and not isinstance(request.body, (str, bytes, bytearray, memoryview)):
            if body_position is None or not hasattr(request.body, "seek"):
                raise UnrewindableBodyError("Request body is not replayable", request=request)
            request.body.seek(body_position)
        attempted = True
        try:
            response = adapter._send_once(request, stream, bounded_timeout(timeout, control),
                verify, cert, proxies, http1, http3, control=control)
        except (ConnectionError, ReadTimeout) as error:
            control.remaining("retry")
            http1 |= getattr(error, "_necko_http1", False)
            http3 &= not getattr(error, "_necko_direct", False)
            if getattr(error, "_necko_restartable", False) or unsent_reset(error):
                continue
            raise
        try:
            control.remaining("response")
            if response.status_code == 421 and getattr(response, "_necko_coalesced", False):
                transport = response._necko_transport
                from .coalescing_snapshot import deny
                deny(transport, urlsplit(request.url).hostname)
                response.close()
                continue
            # Explicitly return HTTP status responses without .content/status retry.
            response.raw.retries = adapter.max_retries
            return response
        except Exception:
            response.close()
            raise
