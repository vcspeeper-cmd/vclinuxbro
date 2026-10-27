"""Per-connection H2/H3 command and exchange admission. No second queue."""
NORMAL_LIMIT = 16
CANCEL_LIMIT = 16
EXCHANGE_LIMIT = 16
COMMAND_COST = 256 * 1024
TOTAL_COST = 4 * 1024 * 1024


class AdmissionError(Exception):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


class Budget:
    def __init__(self):
        self.normal = 0
        self.cancel = 0
        self.live = 0
        self.bytes = 0
        self.closed = False


def preview(method, url, headers, h3=False):
    total = len(method.encode("utf-8")) + len(url.encode("utf-8"))
    for key, value in headers.items():
        size = len(value) if isinstance(value, (bytes, str)) else len(str(value))
        if h3:
            size = 4 * ((size + 2) // 3)
        total += len(str(key)) + size + 32
        if total > COMMAND_COST:
            raise AdmissionError("command-cost")
    return total


def measure(arguments):
    pending, total = list(arguments.values()), 0
    while pending:
        value = pending.pop()
        if isinstance(value, (bytes, bytearray)):
            total += len(value)
        elif isinstance(value, str):
            total += len(value.encode("utf-8"))
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            total += 8 * len(value)
            pending.extend(value)
        if total > COMMAND_COST:
            return total
    return total


def open_exchange(connection, exchange):
    with connection._command_lock:
        budget = connection._ruyijs_budget
        if budget.closed or connection.closed or getattr(exchange, "_ruyijs_live", False):
            raise AdmissionError("exchange")
        if budget.live >= EXCHANGE_LIMIT:
            raise AdmissionError("exchange")
        budget.live += 1
        exchange._ruyijs_live = True


def reserve_normal(connection, exchange, arguments):
    cost = measure(arguments)
    if cost > COMMAND_COST:
        raise AdmissionError("command-cost")
    with connection._command_lock:
        budget = connection._ruyijs_budget
        if budget.closed or connection.closed:
            raise AdmissionError("closed")
        if getattr(exchange, "_ruyijs_normal_cost", None) is not None:
            raise AdmissionError("normal")
        if budget.normal >= NORMAL_LIMIT or budget.bytes + cost > TOTAL_COST:
            raise AdmissionError("normal")
        budget.normal += 1
        budget.bytes += cost
        exchange._ruyijs_normal_cost = cost


def release_normal(connection, exchange):
    with connection._command_lock:
        cost = getattr(exchange, "_ruyijs_normal_cost", None)
        if cost is None:
            return
        exchange._ruyijs_normal_cost = None
        budget = connection._ruyijs_budget
        if budget.normal:
            budget.normal -= 1
            budget.bytes = max(0, budget.bytes - cost)


def release_live(budget, exchange):
    """Return the exchange slot. Only cancel paths retire a live exchange."""
    if getattr(exchange, "_ruyijs_live", False):
        exchange._ruyijs_live = False
        if budget.live:
            budget.live -= 1


def finish_call(connection, call):
    exchange = call.exchange
    with connection._command_lock:
        budget = connection._ruyijs_budget
        if call.operation != "cancel":
            cost = getattr(exchange, "_ruyijs_normal_cost", None)
            if cost is not None:
                exchange._ruyijs_normal_cost = None
                if budget.normal:
                    budget.normal -= 1
                    budget.bytes = max(0, budget.bytes - cost)
        elif getattr(exchange, "_ruyijs_cancel_call", None) is call:
            exchange._ruyijs_cancel_call = None
            exchange._ruyijs_cancel_reserved = False
            if budget.cancel:
                budget.cancel -= 1
            release_live(budget, exchange)
    call.arguments = None


def cancel(connection, exchange, call_type, wake):
    with connection._command_lock:
        if getattr(exchange, "_ruyijs_cancel_call", None) is not None:
            exchange.closed = True
            return
        live = getattr(exchange, "_ruyijs_live", False) or exchange.stream_id is not None
        exchange.closed = True
        if not live:
            return
        budget = connection._ruyijs_budget
        if connection.closed or budget.closed:
            release_live(budget, exchange)
            return
        if budget.cancel >= CANCEL_LIMIT:
            raise AdmissionError("cancel")
        budget.cancel += 1
        exchange._ruyijs_cancel_reserved = True
    call = call_type("cancel", exchange, None)
    with connection._command_lock:
        queued = getattr(exchange, "_ruyijs_cancel_call", None)
        if connection.closed or queued is not None:
            if getattr(exchange, "_ruyijs_cancel_reserved", False) and queued is None:
                connection._ruyijs_budget.cancel -= 1
                exchange._ruyijs_cancel_reserved = False
            # No cancel call reaches finish_call on a closed connection, so the
            # exchange slot has to be retired here. When another thread already
            # queued one, that call's finish_call owns the release instead.
            if queued is None:
                release_live(connection._ruyijs_budget, exchange)
            return
        exchange._ruyijs_cancel_call = call
        connection.commands.put(call)
    wake()


def seal(connection):
    connection._ruyijs_budget.closed = True
