"""Host scheduling requests; all clocks and task state belong to js.exe."""


class SchedulingControl:
    def _event_loop(self, action, **fields):
        self._callbacks.guard()
        with self._lock:
            return self._request("event_loop", action=action, **fields)

    def configure_clock(self, mode, epoch_ms=None):
        """Configure system/logical time before page work or timer advancement.

        Logical mode requires an explicit integer epoch in milliseconds.
        The runtime validates inputs atomically and returns its clock state.
        """
        fields = {"mode": mode}
        if epoch_ms is not None:
            fields["epoch_ms"] = epoch_ms
        return self._event_loop("configure", **fields)

    def clock_status(self):
        """Read the current clock and pending timer count without running timers."""
        return self._event_loop("status")

    def configure_timer_replay(self, enabled=True):
        """Enable explicit host timer selection before page execution."""
        return self._event_loop("configure_timer_replay", enabled=enabled)

    def pending_timers(self):
        """Read live public timers with Context-local realm/task IDs and due times."""
        return self._event_loop("pending_timers")

    def timer_realm(self, target="window"):
        """Resolve a live installed Window to its Context-local timer realm ID.

        Requires timer replay enabled. The host expression may execute script
        and freezes configuration; no timer handle is consumed by the lookup.
        """
        self._callbacks.guard()
        with self._lock:
            return self._request("timer_realm", target=target)

    def configure_message_replay(self, enabled=True):
        return self._event_loop("configure_message_replay", enabled=enabled)

    def pending_messages(self):
        return self._event_loop("pending_messages")

    def run_message(self, task_id, source_realm, target_realm, sequence, kind, at_ms):
        return self._event_loop("run_message", task_id=task_id, source_realm=source_realm,
                                target_realm=target_realm, sequence=sequence, kind=kind, at_ms=at_ms)

    def run_timer(self, task_id, expected_firings, at_ms):
        """Select one due timer in enabled logical mode, then run its engine jobs.

        at_ms is absolute owner elapsed time. This does not select messages,
        external completions or Worker tasks, and can reorder ready timers.
        """
        return self._event_loop("run_timer", task_id=task_id,
                                expected_firings=expected_firings, at_ms=at_ms)

    def configure_navigation_replay(self, documents):
        """Set {url, fields} measurements before execution, at most 32 documents.

        Exact fragment-free URLs bind by occurrence. Fields may contain
        requestStart, responseStart, responseEnd, transferSize, encodedBodySize,
        decodedBodySize and loadEventEnd. Completion values become visible at
        the original load completion; this does not restore task timing.
        """
        return self._event_loop("configure_navigation_replay", documents=documents)

    def navigation_replay_status(self):
        """Return planned URL, field count and binding status without advancing time."""
        return self._event_loop("navigation_replay_status")

    def advance(self, ms, step_ms=16.667, max_tasks=100000):
        """Advance the runtime clock, running bounded macro tasks and engine jobs.

        Logical time advances without wall-clock waiting. System mode waits.
        An exhausted budget returns the actual stopping point and retains work.
        """
        return self._event_loop("advance", ms=ms, step_ms=step_ms, max_tasks=max_tasks)

    def drain_timers(self, max_tasks=100000):
        """Run currently due timers without manually advancing the clock."""
        return self._event_loop("drain", max_tasks=max_tasks)

    def sleep(self, ms, step_ms=16.667, max_tasks=100000):
        """Advance with the selected clock mode; logical mode does not wait."""
        return self.advance(ms, step_ms=step_ms, max_tasks=max_tasks)

    def tick(self, ms=0):
        """Advance with a one-task budget and report the actual stopping time."""
        return self.advance(ms, max_tasks=1)
