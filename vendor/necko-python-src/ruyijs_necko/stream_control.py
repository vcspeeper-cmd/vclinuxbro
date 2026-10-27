"""Request-scoped signals for existing protocol owners and their finite waits."""


def timeout(exchange, limit, phase):
    control = getattr(exchange, "_ruyijs_control", None)
    if control is None:
        return limit
    remaining = control.remaining(phase)
    return remaining if limit is None else min(limit, remaining)


def attach(exchange, control):
    exchange._ruyijs_control = control
    exchange._ruyijs_registration = None
    if control is not None:
        exchange._ruyijs_registration = control.register(lambda: exchange.connection.cancel(exchange))
        control.remaining("stream-create")


def wait(exchange, event, limit, phase):
    control = getattr(exchange, "_ruyijs_control", None)
    if control is None:
        return event.wait(limit)
    return control.wait(event, phase=phase, timeout=limit)


def check(exchange, phase):
    control = getattr(exchange, "_ruyijs_control", None)
    if control is not None:
        control.remaining(phase)


def detach(exchange):
    registration = getattr(exchange, "_ruyijs_registration", None)
    if registration is not None:
        registration.close()
        exchange._ruyijs_registration = None


def close(exchange):
    detach(exchange)
    exchange.connection.cancel(exchange)
    call = getattr(exchange, "_ruyijs_cancel_call", None)
    if call is not None and not call.done.wait(1):
        raise TimeoutError("Protocol stream cancellation cleanup incomplete")


def wait_call(exchange, call, limit):
    try:
        if not wait(exchange, call.done, limit, "stream-" + call.operation):
            raise TimeoutError("HTTP stream operation timed out")
        check(exchange, "stream-" + call.operation)
    except Exception:
        call.cancelled = True
        exchange.connection.cancel(exchange)
        raise


class H1Control:
    """Only an exclusively leased H1 socket may receive request-level interrupt."""
    def __init__(self, exchange, control):
        self.control = control
        transport = exchange.transport
        while hasattr(transport, "transport"):
            transport = transport.transport
        self.socket = transport
        self.previous = transport.attempt
        self.limit = transport.timeout
        self.registration = None
        transport.attempt = self
        try:
            self.registration = control.register(transport.interrupt)
            control.remaining("http1-start")
        except Exception:
            self.close()
            raise

    def remaining(self):
        remaining = self.control.remaining("http1-io")
        return remaining if self.limit is None else min(remaining, self.limit)

    def close(self):
        if self.registration is not None:
            self.registration.close()
            self.registration = None
        if self.socket.attempt is self:
            self.socket.attempt = self.previous
