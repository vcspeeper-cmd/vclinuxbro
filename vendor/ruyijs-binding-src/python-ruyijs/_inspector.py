"""Debug-mode API log. js.exe owns the monitor state, policy and bounds."""


class InspectorControl:
    """Read the debug monitor's record of install-registered member accesses.

    The log is empty unless the Context was created with ``mode="debug"``. Only
    members a production installer registered are observed; a property a script
    defines later is not wrapped, so it produces no entry (see deviation D9).
    """

    def _api_log(self, action, **fields):
        self._callbacks.guard()
        with self._lock:
            return self._request("api_log", action=action, **fields)

    def peek_api_log(self, filter=None):
        """Return recorded entries without draining them."""
        return self._api_log("peek", **({} if filter is None else {"filter": filter}))

    def get_api_log(self, filter=None):
        """Return recorded entries and drain the log."""
        return self._api_log("get", **({} if filter is None else {"filter": filter}))

    def clear_api_log(self):
        """Drop every recorded entry and report the remaining count."""
        return self._api_log("clear")
