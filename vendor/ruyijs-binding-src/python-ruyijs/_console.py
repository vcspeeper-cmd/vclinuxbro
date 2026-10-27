"""Console log reads; the bounded ring lives in the console domain owner."""


class ConsoleControl:
    def _console(self, action, **fields):
        self._callbacks.guard()
        with self._lock:
            return self._request("console", action=action, **fields)

    def console_log(self, clear=False):
        """Return the records the page wrote, oldest first, as a list of dicts.

        Each record has realm (Window realm id), method, level, text, indent and
        stack. The ring is bounded, so it holds the newest records only. With
        clear=True the same command also drops every realm's records, which is
        the atomic read-and-drain used by long-running hosts.
        """
        if not isinstance(clear, bool):
            raise TypeError("clear must be a boolean")
        return self._console("log", clear=clear)["entries"]

    def clear_console(self):
        """Drop every recorded console call and return the empty log."""
        return self._console("clear")["entries"]
