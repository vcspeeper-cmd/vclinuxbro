"""Allocation-free token/quota pass over bounded worker JSON bytes; no AST."""
MAX_CONTROL = 262144
MAX_NODES = 4096
MAX_DEPTH = 8


def _hex4(data, offset):
    if offset + 4 > len(data):
        raise ValueError("Truncated JSON escape")
    value = 0
    for index in range(offset, offset + 4):
        byte = data[index]
        digit = (byte - 48 if 48 <= byte <= 57 else byte - 55 if 65 <= byte <= 70
                 else byte - 87 if 97 <= byte <= 102 else -1)
        if not 0 <= digit <= 15:
            raise ValueError("Invalid JSON escape")
        value = value * 16 + digit
    return value


def _string_end(data, offset):
    while offset < len(data):
        byte = data[offset]
        offset += 1
        if byte == 34:
            return offset
        if byte < 32:
            raise ValueError("Unescaped JSON control character")
        if byte != 92:
            continue
        if offset == len(data):
            raise ValueError("Truncated JSON escape")
        escaped = data[offset]
        offset += 1
        if escaped in (34, 92, 47, 98, 102, 110, 114, 116):
            continue
        if escaped != 117:
            raise ValueError("Invalid JSON escape")
        value = _hex4(data, offset)
        offset += 4
        if 0xD800 <= value <= 0xDBFF:
            if not data.startswith(b"\\u", offset):
                raise ValueError("Missing low surrogate")
            low = _hex4(data, offset + 2)
            if not 0xDC00 <= low <= 0xDFFF:
                raise ValueError("Invalid low surrogate")
            offset += 6
        elif 0xDC00 <= value <= 0xDFFF:
            raise ValueError("Isolated low surrogate")
    raise ValueError("Unterminated JSON string")


def preflight(data):
    if type(data) is not bytes or not 1 <= len(data) <= MAX_CONTROL:
        raise ValueError("Worker JSON size/type invalid")
    if data.startswith(b"\xef\xbb\xbf"):
        raise ValueError("JSON BOM forbidden")
    offset = depth = nodes = 0
    while offset < len(data):
        byte = data[offset]
        if byte in (9, 10, 13, 32, 44, 58):
            offset += 1
            continue
        if byte in (93, 125):
            depth -= 1
            if depth < 0:
                raise ValueError("Invalid JSON container")
            offset += 1
            continue
        nodes += 1
        if nodes > MAX_NODES:
            raise ValueError("Worker JSON node limit")
        if byte in (91, 123):
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError("Worker JSON depth limit")
            offset += 1
        elif byte == 34:
            offset = _string_end(data, offset + 1)
        elif 48 <= byte <= 57:
            start, value = offset, 0
            while offset < len(data) and 48 <= data[offset] <= 57:
                value = value * 10 + data[offset] - 48
                if value > 4294967295 or (offset > start and data[start] == 48):
                    raise ValueError("Noncanonical JSON unsigned32")
                offset += 1
        elif data.startswith(b"true", offset):
            offset += 4
        elif data.startswith(b"false", offset):
            offset += 5
        elif data.startswith(b"null", offset):
            offset += 4
        else:
            raise ValueError("Invalid JSON token")
    if depth:
        raise ValueError("Unclosed JSON container")
    return nodes
