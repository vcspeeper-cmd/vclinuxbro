"""ruyijs-style values encoded as DTOs on the existing typed protocol."""

from ._jsvalue import JSObject, JSFunction
from ._values import encode


def decode_public(context, packet, depth=0):
    if depth > 128 or not isinstance(packet, list) or len(packet) != 2:
        raise ValueError("invalid public value packet")
    kind, value = packet
    if kind == "scalar":
        return value
    if kind == "bytes":
        return bytes.fromhex(value)
    if kind in ("reference", "function"):
        if type(value) is not int or not 0 < value <= 9007199254740991:
            raise ValueError("invalid public reference")
        cls = JSFunction if kind == "function" else JSObject
        return cls(context, value)
    if kind == "missing":
        raise AttributeError("JavaScript object has no such attribute")
    if kind == "array":
        return [decode_public(context, v, depth + 1) for v in value]
    if kind == "object":
        return {k: decode_public(context, v, depth + 1) for k, v in value}
    raise ValueError("unknown public value packet")


def encode_public(context, value, callbacks=None):
    active, nodes = set(), 0

    def visit(item, depth):
        nonlocal nodes
        nodes += 1
        if nodes > 10000 or depth > 64:
            raise ValueError("host argument traversal limit exceeded")
        if isinstance(item, JSObject):
            if item._context is not context:
                raise ValueError("JavaScript references must belong to the same Context")
            return {"kind": "reference", "value": item._reference}
        if callable(item) and callbacks is not None:
            if len(context._callbacks.functions) + len(callbacks) >= 100000:
                raise ValueError("host callback limit exceeded")
            name = getattr(item, "__name__", type(item).__name__)
            if not isinstance(name, str):
                raise TypeError("host callback name must be a string")
            context._host_callback_sequence += 1
            key = "\0callback:" + str(context._host_callback_sequence)
            callbacks[key] = item
            return {"kind": "callback", "value": {"key": key, "name": name}}
        if not isinstance(item, (list, tuple, dict)):
            return {"kind": "value", "value": encode(item)}
        if id(item) in active:
            raise ValueError("cyclic host argument")
        active.add(id(item))
        try:
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise TypeError("host object keys must be strings")
                return {"kind": "object", "value": [[k, visit(v, depth + 1)] for k, v in item.items()]}
            return {"kind": "array", "value": [visit(v, depth + 1) for v in item]}
        finally:
            active.remove(id(item))

    return visit(value, 0)
