"""Decode the runtime's explicit value protocol without guessing types."""

import math
import re


class _Undefined:
    def __repr__(self):
        return "UNDEFINED"


UNDEFINED = _Undefined()


class JSError(RuntimeError):
    """An exception produced by JavaScript, with its original category and stack."""

    def __init__(self, name, message, stack=""):
        self.name, self.message, self.stack = name, message, stack
        super().__init__(f"{name}: {message}")


def decode(node, depth=0):
    if depth > 128 or not isinstance(node, dict):
        raise ValueError("invalid ruyijs typed value")
    kind, value = node.get("type"), node.get("value")
    if kind == "undefined":
        return UNDEFINED
    if kind == "null":
        return None
    if kind == "boolean" and isinstance(value, bool):
        return value
    if kind == "string" and isinstance(value, str):
        return value
    if kind == "number":
        if type(value) in (int, float) and math.isfinite(value):
            return value
        special = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf, "-0": -0.0}
        if isinstance(value, str) and value in special:
            return special[value]
    if kind == "bigint" and isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
        digits = value.lstrip("-")
        integer = 0
        for offset in range(0, len(digits), 1000):
            part = digits[offset:offset + 1000]
            integer = integer * (10 ** len(part)) + int(part)
        return -integer if value.startswith("-") else integer
    if kind == "array" and isinstance(value, list):
        return [decode(item, depth + 1) for item in value]
    if kind == "object" and isinstance(value, list):
        result = {}
        for pair in value:
            if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
                raise ValueError("invalid ruyijs object entry")
            key, item = pair
            if key in result:
                raise ValueError("duplicate ruyijs object key")
            result[key] = decode(item, depth + 1)
        return result
    raise ValueError(f"invalid ruyijs value type: {kind!r}")


def _integer_text(value):
    sign, value = ("-", -value) if value < 0 else ("", value)
    chunks = []
    while value >= 10 ** 1000:
        value, remainder = divmod(value, 10 ** 1000)
        chunks.append(str(remainder).zfill(1000))
    return sign + str(value) + "".join(reversed(chunks))


def encode(value):
    """Python integers beyond JS safe-integer range become BigInt."""
    active, nodes = set(), 0

    def visit(item, depth):
        nonlocal nodes
        nodes += 1
        if nodes > 100000 or depth > 64:
            raise ValueError("value exceeds ruyijs traversal limit")
        if item is UNDEFINED:
            return {"type": "undefined"}
        if item is None:
            return {"type": "null"}
        if isinstance(item, bool):
            return {"type": "boolean", "value": item}
        if isinstance(item, str):
            return {"type": "string", "value": item}
        if isinstance(item, int):
            if abs(item) > 9007199254740991:
                return {"type": "bigint", "value": _integer_text(item)}
            return {"type": "number", "value": item}
        if isinstance(item, float):
            number = ("NaN" if math.isnan(item) else "Infinity" if item == math.inf else
                      "-Infinity" if item == -math.inf else "-0" if item == 0 and
                      math.copysign(1, item) < 0 else item)
            return {"type": "number", "value": number}
        if not isinstance(item, (list, tuple, dict)):
            raise TypeError(f"unsupported ruyijs value: {type(item).__name__}")
        if id(item) in active:
            raise ValueError("cyclic ruyijs value")
        active.add(id(item))
        try:
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise TypeError("ruyijs object keys must be strings")
                return {"type": "object", "value": [[key, visit(entry, depth + 1)]
                                                    for key, entry in item.items()]}
            return {"type": "array", "value": [visit(entry, depth + 1) for entry in item]}
        finally:
            active.remove(id(item))

    return visit(value, 0)
