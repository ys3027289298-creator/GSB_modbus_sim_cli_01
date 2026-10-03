"""Scheduled-task boundary tests: exception containment and shutdown races."""
import threading
import unittest

from modbus_sim.utils.backgroundJob import BackgroundJob
from modbus_sim.utils.errors import ResponseTimeoutError


class BackgroundJobTest(unittest.TestCase):
    def test_failing_tick_does_not_kill_the_job(self):
        calls = []

        def tick():
            calls.append(1)
            if len(calls) <= 2:
                raise ResponseTimeoutError("device did not answer")

        job = BackgroundJob("sim", 0.01, tick)
        job.start()
        try:
            deadline = threading.Event()
            self.assertTrue(not deadline.wait(0.15))
            self.assertGreaterEqual(len(calls), 3)
        finally:
            job.cancel(timeout=1)
        self.assertFalse(job.is_alive())

    def test_cancel_while_tick_running_joins_cleanly(self):
        started = threading.Event()
        release = threading.Event()

        def slow_tick():
            started.set()
            release.wait(2)

        job = BackgroundJob("sim", 0.01, slow_tick)
        job.start()
        started.wait(1)
        # Tick is still running: cancel reports it via join timeout instead
        # of returning while state is being mutated.
        job.cancel(timeout=0.1)
        self.assertTrue(job.is_alive())
        release.set()
        job.join(1)
        self.assertFalse(job.is_alive())

    def test_cancel_before_start_and_double_cancel(self):
        job = BackgroundJob("sim", 1, lambda: None)
        job.cancel()  # must not raise on an unstarted thread
        job.cancel()

    def test_cancel_during_interval_wait_stops_promptly(self):
        job = BackgroundJob("sim", 10, lambda: None)
        job.start()
        job.cancel(timeout=1)
        self.assertFalse(job.is_alive())


if __name__ == "__main__":
    unittest.main()
