"""
Copyright (c) 2016 Riptide IO, Inc. All Rights Reserved.

Unified error types and boundary validators.

Every cross-boundary failure in the simulator is reported as a subclass of
:class:`ModbusSimError` so callers can handle the four boundaries uniformly:

  * register address space -> AddressOutOfRangeError / ZeroLengthRequestError
  * function codes         -> UnsupportedFunctionCodeError
  * connection lifecycle   -> ConnectionLostError / ReconnectExhaustedError
  * scheduled tasks        -> ResponseTimeoutError
"""
from __future__ import absolute_import, unicode_literals


class ModbusSimError(Exception):
    """Base class for all simulator boundary errors."""
    boundary = "unknown"

    def __init__(self, message="", **context):
        self.context = context
        super(ModbusSimError, self).__init__(message)


class AddressOutOfRangeError(ModbusSimError):
    """Read/write outside the configured register block."""
    boundary = "address_space"


class ZeroLengthRequestError(ModbusSimError):
    """Request or block with zero (or negative) length."""
    boundary = "address_space"


class UnsupportedFunctionCodeError(ModbusSimError):
    """Function code the simulator does not implement."""
    boundary = "function_code"


class ConnectionLostError(ModbusSimError):
    """Transport dropped before/during a request."""
    boundary = "connection_lifecycle"


class ReconnectExhaustedError(ConnectionLostError):
    """Bounded reconnect/retry policy gave up; state is left intact."""
    boundary = "connection_lifecycle"


class RequestCancelledError(ModbusSimError):
    """In-flight request was cancelled (e.g. after a timeout)."""
    boundary = "connection_lifecycle"


class ResponseTimeoutError(ModbusSimError):
    """No response within the configured deadline."""
    boundary = "scheduled_task"


READ_FUNCTION_CODES = frozenset([1, 2, 3, 4])
WRITE_FUNCTION_CODES = frozenset([5, 6, 15, 16])
SUPPORTED_FUNCTION_CODES = READ_FUNCTION_CODES | WRITE_FUNCTION_CODES

FUNCTION_CODE_BLOCKS = {
    1: "coils",
    2: "discrete_inputs",
    3: "holding_registers",
    4: "input_registers",
    5: "coils",
    6: "holding_registers",
    15: "coils",
    16: "holding_registers",
}


def validate_function_code(code):
    if code not in SUPPORTED_FUNCTION_CODES:
        raise UnsupportedFunctionCodeError(
            "Unsupported function code: %r (supported: %s)" % (
                code, sorted(SUPPORTED_FUNCTION_CODES)),
            function_code=code)
    return code


def validate_block(start, size):
    if start < 0:
        raise AddressOutOfRangeError(
            "Block start %s is negative" % start, start=start, size=size)
    if size <= 0:
        raise ZeroLengthRequestError(
            "Block size %s is not positive" % size, start=start, size=size)


def validate_address(address, count, block_start, block_size):
    """Validate [address, address+count) against a block's bounds."""
    if count <= 0:
        raise ZeroLengthRequestError(
            "Request length %s is not positive" % count,
            address=address, count=count)
    end = address + count
    if address < block_start or end > block_start + block_size:
        raise AddressOutOfRangeError(
            "Address range [%s, %s) is outside block [%s, %s)" % (
                address, end, block_start, block_start + block_size),
            address=address, count=count,
            block_start=block_start, block_size=block_size)
