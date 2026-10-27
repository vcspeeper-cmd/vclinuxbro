"""Host callback dispatch and same-context reentry protection."""

import queue
import threading
import time
import traceback

from ._values import JSError, decode, encode


class ReentrantError(RuntimeError):
    """A host callback attempted to enter its waiting JavaScript context."""


def _error_reply(error):
    def bounded(value, limit, fallback):
        try:
            return str(value).encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")
        except BaseException:
            return fallback
    name = error.name if isinstance(error, JSError) else type(error).__name__
    message = error.message if isinstance(error, JSError) else error
    try:
        stack = error.stack if isinstance(error, JSError) else "".join(traceback.format_exception(error))
    except BaseException:
        stack = ""
    return {"ok": False, "error": {"name": bounded(name, 128, "HostError"),
            "message": bounded(message, 4096, "Host callback failed"),
            "stack": bounded(stack, 16384, "")}}


class Callbacks:
    def __init__(self):
        self.functions = {}
        self.active = threading.Event()

    def guard(self):
        if self.active.is_set():
            raise ReentrantError("same-context calls during a host callback are rejected")

    def dispatch(self, frame, deadline):
        if type(frame.get("callId")) is not int or not isinstance(frame.get("name"), str):
            raise ValueError("invalid ruyijs callback identity")
        arguments = decode(frame.get("args"))
        if not isinstance(arguments, list):
            raise ValueError("ruyijs callback arguments must be an array")
        results = queue.Queue(maxsize=1)
        self.active.set()

        def run():
            try:
                function = self.functions.get(frame["name"])
                if function is None:
                    raise NameError(f"unknown host function: {frame['name']}")
                result = {"ok": True, "value": encode(function(*arguments))}
            except BaseException as error:
                result = _error_reply(error)
            finally:
                self.active.clear()
            results.put(result)

        threading.Thread(target=run, name="ruyijs-host-callback", daemon=True).start()
        try:
            result = results.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            raise TimeoutError("ruyijs host callback timed out") from None
        return {"op": "callback-result", "callId": frame["callId"], **result}
