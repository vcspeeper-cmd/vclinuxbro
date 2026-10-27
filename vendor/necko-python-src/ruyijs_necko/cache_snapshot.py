"""Stateless operations on the original adapter's finite immutable cache roots."""
import math
import time

EMPTY = ((), 0)
MAX_ITEMS = 64


def text_cost(value, limit):
    if type(value) is bytes:
        return len(value)
    if type(value) is not str or len(value) > limit:
        return limit + 1
    total = 0
    for char in value:
        point = ord(char)
        if 0xd800 <= point <= 0xdfff:
            return limit + 1
        total += 1 if point < 128 else 2 if point < 2048 else 3 if point < 65536 else 4
        if total > limit:
            break
    return total


def key_cost(key):
    if type(key) is not tuple or len(key) != 7:
        return 32769
    total = 0
    for part in key:
        if type(part) in (str, bytes):
            total += text_cost(part, 32768 - total)
        elif type(part) not in (int, bool, type(None)):
            return 32769
        if total > 32768:
            break
    return total


def _commit(state, field, lock, previous, replacement):
    with lock:
        if state[field] is previous:
            state[field] = replacement
            return True
    return False


def _root(rows):
    return tuple(rows), sum(row[3] for row in rows)


def _apply(owner, field, key, mode, entry=None, expected=None):
    state = owner.__dict__
    lock = owner._token_lock if field == "_quic_tokens" else owner._lock
    limit = 1048576 if field == "_quic_tokens" else 262144
    for _ in range(4):
        previous = state[field]
        if previous is None:
            return None
        now = time.monotonic()
        rows, found = [], None
        for row in previous[0]:
            if row[2] <= now:
                continue
            if row[0] == key:
                found = row
            else:
                rows.append(row)
        result = None if found is None else (found[1], found[2])
        if mode == "read" and found is not None:
            rows.append(found)
        elif mode == "drop" and found is not None and expected is not None and result != expected:
            rows.append(found)
        elif mode == "store":
            used = sum(row[3] for row in rows)
            while rows and (len(rows) >= MAX_ITEMS or used + entry[3] > limit):
                used -= rows.pop(0)[3]
            rows.append(entry)
        replacement = _root(rows)
        if _commit(state, field, lock, previous, replacement):
            return result
    # Optional metadata stays bounded under sustained reentrancy/contention.
    # A cold cache is preferable to retaining a superseded overlarge candidate.
    clear(owner, field)
    return None


def lookup(owner, field, key):
    if key_cost(key) > 32768:
        return None
    try:
        return _apply(owner, field, key, "read")
    except MemoryError:
        return None


def put(owner, field, key, value, age):
    cost = key_cost(key)
    if field == "_quic_tokens":
        payload = text_cost(value, 65536)
        accepted = type(value) is str and payload <= 65536 and cost <= 32768
    else:
        payload = text_cost(value[0], 32768)
        accepted = cost + payload <= 32768
    if type(age) not in (int, float) or not math.isfinite(age) or age <= 0:
        accepted = False
    if not accepted:
        drop(owner, field, key)
        return
    try:
        entry = (key, value, time.monotonic() + age, cost + payload)
        _apply(owner, field, key, "store", entry)
    except MemoryError:
        pass


def drop(owner, field, key, expected=None):
    if key_cost(key) > 32768:
        return
    try:
        _apply(owner, field, key, "drop", expected=expected)
    except MemoryError:
        pass


def clear(owner, field, *, closing=False):
    state = owner.__dict__
    lock = owner._token_lock if field == "_quic_tokens" else owner._lock
    with lock:
        previous = state[field]
        if closing or state[field] is not None:
            state[field] = None if closing else EMPTY
    del previous
