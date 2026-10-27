"""One immutable ORIGIN/421 state on each original physical transport."""
from .cache_snapshot import text_cost

INITIAL = (None, (), 0, 0, False)
DISABLED = ((), (), 0, 0, True)


def allowed(transport, host, port):
    state = transport._coalescing_state
    if state[4] or host in state[1]:
        return False, False
    origins = state[0]
    if origins is not None:
        return (host, port) in origins, True
    return True, False


def _commit(transport, lock, previous, replacement):
    state = transport.__dict__
    with lock:
        if state["_coalescing_state"] is previous:
            state["_coalescing_state"] = replacement
            return True
    return False


def disable(transport, lock):
    state = transport.__dict__
    with lock:
        previous = state["_coalescing_state"]
        state["_coalescing_state"] = DISABLED
    del previous


def _insert(transport, lock, host, port, origins):
    cost = text_cost(host, 65536)
    if type(host) is not str:
        raise ValueError("Coalescing hostname type")
    index = 0 if origins else 1
    entry = (host, port) if origins else host
    for _ in range(4):
        previous = transport._coalescing_state
        if previous[4]:
            return
        rows = previous[index] or ()
        if entry in rows:
            return
        if len(rows) == 256 or cost > 65536 - previous[index + 2]:
            disable(transport, lock)
            return
        updated = rows + (entry,)
        if origins:
            replacement = (updated, previous[1], previous[2] + cost, previous[3], False)
        else:
            replacement = (previous[0], updated, previous[2], previous[3] + cost, False)
        if _commit(transport, lock, previous, replacement):
            return
    disable(transport, lock)


def add_origin(protocol, host, port):
    _insert(protocol.transport, protocol._command_lock, host, port, True)


def origin_frame(protocol):
    transport = protocol.transport
    for _ in range(4):
        previous = transport._coalescing_state
        if previous[4] or previous[0] is not None:
            return
        replacement = ((), previous[1], 0, previous[3], False)
        if _commit(transport, protocol._command_lock, previous, replacement):
            return
    disable(transport, protocol._command_lock)


def deny(transport, host):
    protocol = getattr(transport, "h2", None) or transport
    try:
        _insert(transport, protocol._command_lock, host, None, False)
    except MemoryError:
        disable(transport, protocol._command_lock)


def finish(protocol):
    transport = getattr(protocol, "transport", None) or protocol
    disable(transport, protocol._command_lock)
