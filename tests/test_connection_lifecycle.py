"""
Connection-lifecycle boundary tests.

All races are reproduced deterministically with VirtualConnection hooks and
events - no timing guesses.
"""
import threading
import time
import unittest

from modbus_sim.simulation.connection import (
    ConnectionManager,
    ModbusRequest,
    RegisterFile,
    VirtualConnection,
)
from modbus_sim.utils.errors import (
    ConnectionLostError,
    ReconnectExhaustedError,
    ResponseTimeoutError,
)

BLOCK = "holding_registers"


def make_register_file(size=10):
    registers = RegisterFile()
    registers.add_block(BLOCK, 0, size, default=0)
    return registers


def no_sleep(_seconds):
    return None


class MidWriteDisconnectRaceTest(unittest.TestCase):
    """A write of [1, 2, 3, 4] is interrupted by a disconnect exactly between
    staging and commit (or between registers in legacy mode)."""

    def setUp(self):
        self.registers = make_register_file()
        self.request = ModbusRequest(16, 0, values=[1, 2, 3, 4],
                                     request_id="write-1")

    def _disconnect_once(self, connection):
        def hook(conn):
            if not hook.fired:
                hook.fired = True
                conn.disconnect()
        hook.fired = False
        return hook

    def test_legacy_connection_pollutes_state(self):
        # Reproduces the race on the legacy (register-by-register) path:
        # the disconnect lands mid-write and leaves partial state behind.
        connection = VirtualConnection(self.registers, transactional=False)
        connection.on_commit = self._disconnect_once(connection)
        connection.connect()
        with self.assertRaises(ConnectionLostError):
            connection.execute(self.request)
        values = self.registers.read(BLOCK, 0, 4)
        self.assertNotEqual(values, (0, 0, 0, 0),
                            "race did not trigger: no register was written")
        self.assertNotEqual(values, (1, 2, 3, 4),
                            "race did not trigger: write completed fully")

    def test_transactional_write_is_all_or_nothing(self):
        connection = VirtualConnection(self.registers)
        connection.on_commit = self._disconnect_once(connection)
        manager = ConnectionManager(connection, max_retries=3,
                                    sleeper=no_sleep)
        result = manager.execute(self.request)
        self.assertEqual(result, 4)
        self.assertEqual(self.registers.read(BLOCK, 0, 4), (1, 2, 3, 4))
        # The interrupted attempt staged nothing; the retry applied once.
        self.assertEqual(connection.applied_requests, ["write-1"])

    def test_interrupted_attempt_leaves_no_partial_state(self):
        connection = VirtualConnection(self.registers)
        connection.on_commit = self._disconnect_once(connection)
        connection.connect()
        with self.assertRaises(ConnectionLostError):
            connection.execute(self.request)
        self.assertEqual(self.registers.read(BLOCK, 0, 4), (0, 0, 0, 0))


class ReconnectPolicyTest(unittest.TestCase):
    def setUp(self):
        self.registers = make_register_file()
        self.connection = VirtualConnection(self.registers)

    def test_retry_then_recover(self):
        self.connection.refuse_connects = 2
        manager = ConnectionManager(self.connection, max_retries=3,
                                    sleeper=no_sleep)
        result = manager.execute(ModbusRequest(3, 0, count=2,
                                               request_id="read-1"))
        self.assertEqual(result, (0, 0))
        self.assertEqual(self.connection.connect_attempts, 3)

    def test_exhaustion_reports_error_and_keeps_state(self):
        self.registers.write(BLOCK, 0, [7, 7, 7])
        self.connection.refuse_connects = 10
        manager = ConnectionManager(self.connection, max_retries=2,
                                    sleeper=no_sleep)
        with self.assertRaises(ReconnectExhaustedError):
            manager.execute(ModbusRequest(3, 0, count=3, request_id="read-2"))
        # Register state survives the outage; the next call re-attempts
        # recovery instead of serving stale data or failing forever.
        self.assertEqual(self.registers.read(BLOCK, 0, 3), (7, 7, 7))
        self.connection.refuse_connects = 0
        self.assertEqual(
            manager.execute(ModbusRequest(3, 0, count=3, request_id="read-3")),
            (7, 7, 7))

    def test_disconnect_between_requests_triggers_single_reconnect(self):
        manager = ConnectionManager(self.connection, max_retries=3,
                                    sleeper=no_sleep)
        manager.execute(ModbusRequest(3, 0, count=1, request_id="r1"))
        self.connection.disconnect()
        manager.execute(ModbusRequest(3, 0, count=1, request_id="r2"))
        self.assertEqual(self.connection.connect_attempts, 2)


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.registers = make_register_file()
        self.connection = VirtualConnection(self.registers)
        self.manager = ConnectionManager(self.connection, max_retries=3,
                                         sleeper=no_sleep)

    def test_repeated_request_id_is_applied_once(self):
        request = ModbusRequest(16, 0, values=[5, 5], request_id="dup-1")
        first = self.manager.execute(request)
        second = self.manager.execute(request)
        self.assertEqual(first, second)
        self.assertEqual(self.connection.applied_requests, ["dup-1"])
        self.assertEqual(self.registers.read(BLOCK, 0, 2), (5, 5))

    def test_failed_request_is_not_cached(self):
        self.connection.refuse_connects = 1
        manager = ConnectionManager(self.connection, max_retries=0,
                                    sleeper=no_sleep)
        request = ModbusRequest(16, 0, values=[9], request_id="dup-2")
        with self.assertRaises(ReconnectExhaustedError):
            manager.execute(request)
        # A retry of the same id after recovery must really execute.
        self.assertEqual(manager.execute(request), 1)
        self.assertEqual(self.registers.read(BLOCK, 0, 1), (9,))


class TimeoutTest(unittest.TestCase):
    def test_timeout_cancels_before_commit_and_allows_safe_retry(self):
        registers = make_register_file()
        connection = VirtualConnection(registers, latency=0.3)
        manager = ConnectionManager(connection, max_retries=3, timeout=0.05,
                                    sleeper=no_sleep)
        request = ModbusRequest(16, 0, values=[1, 1, 1], request_id="slow-1")
        with self.assertRaises(ResponseTimeoutError):
            manager.execute(request)
        # Give the cancelled worker a chance to (wrongly) commit late.
        time.sleep(0.4)
        self.assertEqual(registers.read(BLOCK, 0, 3), (0, 0, 0),
                         "timed-out write leaked into register state")
        # Same request id after the timeout: not cached, executes for real.
        connection.latency = 0.0
        self.assertEqual(manager.execute(request), 3)
        self.assertEqual(registers.read(BLOCK, 0, 3), (1, 1, 1))
        self.assertEqual(connection.applied_requests, ["slow-1"])


if __name__ == "__main__":
    unittest.main()
