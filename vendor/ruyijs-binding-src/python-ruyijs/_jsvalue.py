"""Python views of original JavaScript objects; no copied browser state."""


class JSObject:
    __slots__ = ("_context", "_reference", "__weakref__")

    def __init__(self, context, reference):
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_reference", reference)

    def __del__(self):
        try:
            context = object.__getattribute__(self, "_context")
            reference = object.__getattribute__(self, "_reference")
            if not context.closed:
                context._host_releases.append(reference)
        except (AttributeError, TypeError):
            pass

    def __getattr__(self, key):
        return self._context._host_value(self._reference, "get", key=key)

    def __getitem__(self, key):
        return self.__getattr__(str(key))

    def __setattr__(self, key, value):
        self._context._host_value(self._reference, "set", key=key, value=value)

    def __setitem__(self, key, value):
        self.__setattr__(str(key), value)

    def __delattr__(self, key):
        self._context._host_value(self._reference, "delete", key=key)

    def __delitem__(self, key):
        self.__delattr__(str(key))

    def __contains__(self, key):
        return self._context._host_value(self._reference, "has", key=str(key))

    def __dir__(self):
        return self._context._host_value(self._reference, "keys")

    def __str__(self):
        return self._context._host_value(self._reference, "str")

    def __repr__(self):
        return f"<{type(self).__name__} reference={self._reference}>"

    def to_py(self):
        return self._context._host_value(self._reference, "to_py")


class JSFunction(JSObject):
    __slots__ = ()

    def __call__(self, *args, **kwargs):
        if kwargs:
            raise TypeError("JavaScript functions do not support keyword arguments")
        return self._context._host_value(self._reference, "call", value=args)
