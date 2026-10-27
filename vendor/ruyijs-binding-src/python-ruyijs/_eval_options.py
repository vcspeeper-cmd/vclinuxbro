"""Host argument validation; source coordinates are applied by SpiderMonkey."""

def source_text(value, field):
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text or UTF-8 bytes")
    return value


def source_offset(value, field):
    if not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if not -2147483648 <= value <= 2147483647:
        raise OverflowError(f"{field} must fit a signed 32-bit integer")
    return int(value)
