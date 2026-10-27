"""Bound existing native socket operations; retain native TLS/HTTP framing."""
import ctypes
import time


def budget(transport):
    limit = transport._timeout()
    return None if limit is None else time.monotonic() + limit


def remaining(transport, end):
    limit = transport._timeout()
    value = None if end is None else end - time.monotonic()
    if value is not None and value <= 0:
        raise TimeoutError("Socket operation timed out")
    if limit is not None:
        value = limit if value is None else min(value, limit)
    return .025 if value is None else min(value, .025)


def read(transport, size):
    end = budget(transport)
    buffer = (ctypes.c_uint8 * size)()
    length = ctypes.c_uint32()
    while True:
        delay = remaining(transport, end)
        result = transport.library.necko_socket_read(transport.handle, buffer, size,
            max(1, int(delay * 1000)), ctypes.byref(length))
        remaining(transport, end)
        if result == 5:
            continue
        if result:
            raise OSError(f"Native socket read error {result}")
        return bytes(buffer[:length.value])


def write(transport, data):
    end = budget(transport)
    view = memoryview(data)
    while view:
        remaining(transport, end)
        part = view[:65536]
        buffer = (ctypes.c_uint8 * len(part)).from_buffer_copy(part)
        length = ctypes.c_uint32()
        result = transport.library.necko_socket_write_some(transport.handle, buffer,
            len(part), ctypes.byref(length))
        delay = remaining(transport, end)
        if result == 5:
            time.sleep(min(.01, delay))
            continue
        if result:
            raise OSError(f"Native socket write error {result}")
        if not length.value:
            raise OSError("Native socket write made no progress")
        view = view[length.value:]
