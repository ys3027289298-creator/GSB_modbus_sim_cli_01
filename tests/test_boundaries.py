"""
Register-address-space and function-code boundary tests.
Covers the unified validators, RegisterFile and ModbusSimu (the latter with
an in-memory fake server backend, so modbus_tk is not required).
"""
import unittest

from modbus_sim.simulation import modbus
from modbus_sim.simulation.connection import (
    ModbusRequest,
    RegisterFile,
    VirtualConnection,
    ConnectionManager,
)
from modbus_sim.utils.errors import (
    AddressOutOfRangeError,
    UnsupportedFunctionCodeError,
    ZeroLengthRequestError,
)


class FunctionCodeTest(unittest.TestCase):
    def test_unsupported_function_code(self):
        with self.assertRaises(UnsupportedFunctionCodeError) as caught:
            ModbusRequest(99, 0)
        self.assertEqual(caught.exception.boundary, "function_code")

    def test_supported_function_codes(self):
        for code in (1, 2, 3, 4, 5, 6, 15, 16):
            ModbusRequest(code, 0)


class RegisterFileBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.registers = RegisterFile()
        self.registers.add_block("holding_registers", 0, 10, default=0)

    def test_zero_length_read_and_write(self):
        with self.assertRaises(ZeroLengthRequestError):
            ModbusRequest(3, 0, count=0)
        with self.assertRaises(ZeroLengthRequestError):
            ModbusRequest(16, 0, values=[])

    def test_out_of_bounds_read(self):
        with self.assertRaises(AddressOutOfRangeError):
            self.registers.read("holding_registers", 9, 2)
        with self.assertRaises(AddressOutOfRangeError):
            self.registers.read("holding_registers", -1, 1)

    def test_out_of_bounds_write_leaves_no_partial_state(self):
        with self.assertRaises(AddressOutOfRangeError):
            self.registers.write("holding_registers", 8, [1, 2, 3])
        self.assertEqual(self.registers.read("holding_registers", 0, 10),
                         tuple([0] * 10))

    def test_zero_sized_block_rejected(self):
        with self.assertRaises(ZeroLengthRequestError):
            self.registers.add_block("coils", 0, 0)
        with self.assertRaises(AddressOutOfRangeError):
            self.registers.add_block("bad", -1, 10)

    def test_repeated_writes_do_not_pollute(self):
        for _ in range(5):
            self.registers.write("holding_registers", 2, [1, 2, 3])
        self.assertEqual(self.registers.read("holding_registers", 0, 5),
                         (0, 0, 1, 2, 3))


class ConnectionValidationTest(unittest.TestCase):
    def test_validation_error_is_not_retried(self):
        registers = RegisterFile()
        registers.add_block("holding_registers", 0, 4)
        connection = VirtualConnection(registers)
        manager = ConnectionManager(connection, max_retries=3,
                                    sleeper=lambda _s: None)
        with self.assertRaises(AddressOutOfRangeError):
            manager.execute(ModbusRequest(3, 0, count=5, request_id="bad"))
        self.assertEqual(connection.connect_attempts, 1)
        self.assertEqual(connection.applied_requests, [])


class FakeSlave(object):
    def __init__(self):
        self.blocks = {}

    def add_block(self, name, block_type, start, size):
        self.blocks[name] = {"start": start, "values": [0] * size}

    def remove_block(self, name):
        self.blocks.pop(name, None)

    def remove_all_blocks(self):
        self.blocks.clear()

    def set_values(self, name, address, values):
        block = self.blocks[name]
        offset = address - block["start"]
        block["values"][offset:offset + len(values)] = values

    def get_values(self, name, address, size):
        block = self.blocks[name]
        offset = address - block["start"]
        return tuple(block["values"][offset:offset + size])


class FakeServer(object):
    def __init__(self, *args, **kwargs):
        self._slaves = {}
        self._databank = self
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def add_slave(self, slave_id):
        self._slaves[slave_id] = FakeSlave()

    def get_slave(self, slave_id):
        return self._slaves[slave_id]

    def remove_slave(self, slave_id):
        self._slaves.pop(slave_id, None)

    def remove_all_slaves(self):
        self._slaves.clear()


class ModbusSimuBoundaryTest(unittest.TestCase):
    def setUp(self):
        self._original = modbus.SERVERS.get("tcp")
        modbus.SERVERS["tcp"] = FakeServer
        self.simu = modbus.ModbusSimu(server="tcp", port=5020)

    def tearDown(self):
        if self._original is None:
            modbus.SERVERS.pop("tcp", None)
        else:
            modbus.SERVERS["tcp"] = self._original

    def test_block_validation(self):
        self.simu.add_slave(1)
        with self.assertRaises(ZeroLengthRequestError):
            self.simu.add_block(1, "holding_registers", 4, 0, 0)
        with self.assertRaises(AddressOutOfRangeError):
            self.simu.add_block(1, "coils", 1, -1, 10)

    def test_read_write_bounds_and_idempotency(self):
        self.simu.add_slave(1)
        self.simu.add_block(1, "holding_registers", 4, 0, 10)
        with self.assertRaises(AddressOutOfRangeError):
            self.simu.get_values(1, "holding_registers", 9, 2)
        with self.assertRaises(AddressOutOfRangeError):
            self.simu.set_values(1, "holding_registers", 9, [1, 2])
        with self.assertRaises(ZeroLengthRequestError):
            self.simu.get_values(1, "holding_registers", 0, 0)
        with self.assertRaises(AddressOutOfRangeError):
            self.simu.get_values(1, "unknown_block", 0, 1)
        for _ in range(3):
            self.simu.set_values(1, "holding_registers", 0, [5] * 10)
        self.assertEqual(self.simu.get_values(1, "holding_registers", 0, 10),
                         tuple([5] * 10))


if __name__ == "__main__":
    unittest.main()
