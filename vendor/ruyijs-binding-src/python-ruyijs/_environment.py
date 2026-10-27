"""Environment control requests; validation and browser state live in runtime."""

import math


def _plain_json_object(environment):
    if type(environment) is not dict:
        raise TypeError("environment must be a plain JSON object")
    active, nodes = set(), 0

    def check(value, depth):
        nonlocal nodes
        nodes += 1
        if depth > 64 or nodes > 100000:
            raise ValueError("environment exceeds protocol traversal limits")
        if value is None or type(value) in (str, bool, int):
            return
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError("environment JSON numbers must be finite")
            return
        if type(value) not in (dict, list):
            raise TypeError("environment contains a value outside the JSON protocol")
        if id(value) in active:
            raise ValueError("environment contains a cycle")
        active.add(id(value))
        try:
            if type(value) is dict:
                if any(type(key) is not str for key in value):
                    raise TypeError("environment object keys must be strings")
                values = value.values()
            else:
                values = value
            for item in values:
                check(item, depth + 1)
        finally:
            active.remove(id(value))

    check(environment, 0)


class EnvironmentControl:
    def configure_fingerprint(self, fingerprint):
        """Apply a partial, validated Firefox fingerprint profile to this Context.

        The profile uses the same owner-backed groups and schema as
        ``configure_environment``. Fields are merged atomically; values are
        read from the browser APIs themselves, not injected as page globals.
        Configure it before loading a page so scripts observe one profile.
        """
        return self.configure_environment(fingerprint)

    def configure_environment(self, environment):
        """Atomically merge configuration before the first page operation.

        Supported fields and ranges are validated by the shared runtime owner.
        A rejected configuration preserves existing state and allows retry.
        Returns the resulting environment snapshot.
        """
        self._callbacks.guard()
        _plain_json_object(environment)
        with self._lock:
            return self._request("configure", environment=environment)

    def get_defaults(self):
        """Return the fixed Firefox baseline, independently of this context's overrides."""
        self._callbacks.guard()
        with self._lock:
            return self._request("defaults")
