"""Trusted input is created by the existing native Interaction owner."""


class InputControl:
    def dispatch_input(self, event_type, *, target="document", **init):
        """Dispatch one trusted Mouse/Pointer event and return True on delivery.

        target is a JavaScript expression evaluated in this Context. The event
        is created in that target's realm. Coordinate/flag fields use the native
        event dictionary; timeStamp reads the Context's scheduling clock. No
        layout, hit testing, or automatic compatibility mouse event is generated.
        """
        if not isinstance(event_type, str) or not isinstance(target, str):
            raise TypeError("input type and target must be strings")
        self._callbacks.guard()
        with self._lock:
            return self._request("input", type=event_type, target=target, init=init)

    def click(self, target, *, hold_ms=80):
        """Click the element `target` (a JavaScript expression) as a user does.

        Trusted pointer/mouse events in Firefox's order at the element's center:
        enter/over, move, down (which moves focus), `hold_ms` of running event
        loop, up, then click (a PointerEvent) and its activation - a submit
        button submits its form, a link navigates.
        """
        if not isinstance(target, str):
            raise TypeError("click target must be a string expression")
        return self._user(action="click", target=target, hold_ms=hold_ms)

    def type(self, text, *, target=None, interval_ms=60):
        """Type `text` into the focused element (or focus `target` first).

        Each character is keydown, keypress, beforeinput, the insertion, input
        and keyup, with `interval_ms` of running event loop between characters.
        """
        if not isinstance(text, str) or (target is not None and not isinstance(target, str)):
            raise TypeError("type text and target must be strings")
        fields = {"text": text, "interval_ms": interval_ms}
        if target is not None:
            fields["target"] = target
        return self._user(action="type", **fields)

    def press(self, key, *, target=None):
        """Press one named key: Enter, Backspace, Tab, Escape or an arrow key.

        Enter inserts a line break in a textarea and submits from an input.
        """
        if not isinstance(key, str) or (target is not None and not isinstance(target, str)):
            raise TypeError("press key and target must be strings")
        fields = {"key": key}
        if target is not None:
            fields["target"] = target
        return self._user(action="press", **fields)

    def _user(self, **fields):
        self._callbacks.guard()
        with self._lock:
            return self._request("user", **fields)
