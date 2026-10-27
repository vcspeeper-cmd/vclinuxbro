"""Initial-page RNG replay state; SpiderMonkey retains the native generator."""


class RandomControl:
    def configure_random(self, state0, state1):
        """Restore initial-page Math.random state before page/host execution.

        Each word is an exact 16-digit hexadecimal string. The state is local
        to the initial page realm; child realms and later navigate() documents
        retain independent generators. Use load_page() to replay that page.
        """
        self._callbacks.guard()
        with self._lock:
            return self._request('configure_random', state0=state0, state1=state1)

    def configure_crypto_replay(self, documents):
        """Set ordered {url, values:[hex or UUID record,...]} before execution.

        Each exact document URL consumes one configured occurrence on load.
        Hex strings describe getRandomValues calls; UUID records have
        {method:"randomUUID", value:"..."}. The existing Crypto instance
        reports method/length/exhaustion mismatches. Unplanned documents use OS randomness.
        """
        self._callbacks.guard()
        with self._lock:
            return self._request('configure_crypto_replay', documents=documents)

    def crypto_replay_status(self):
        """Read document binding and call-consumption counts without input bytes."""
        self._callbacks.guard()
        with self._lock:
            return self._request('crypto_replay_status')
