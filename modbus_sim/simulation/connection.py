"""
Copyright (c) 2016 Riptide IO, Inc. All Rights Reserved.

Connection lifecycle management for the simulator.

Three pieces cooperate here:

  * :class:`RegisterFile`      - in-memory register blocks. Every read/write
                                 is fully validated and applied under a lock,
                                 so a write either lands completely or not at
                                 all (address-space boundary).
  * :class:`VirtualConnection` - in-memory transport used for tests and for
                                 running without modbus_tk. It exposes fault
                                 injection knobs (refused connects, latency,
                                 staged-commit hooks, legacy racy writes) that
                                 make races reproducible deterministically.
  * :class:`ConnectionManager` - enforces the connection-lifecycle policy:
                                 bounded reconnect+retry with backoff, then
                                 :class:`ReconnectExhaustedError`; per-request
                                 timeout that cancels before commit; and
                                 request-id de-duplication so retransmits
                                 cannot pollute register state.
"""
from __future__ import absolute_import, unicode_literals

import threading
import time
from collections import OrderedDict

from modbus_sim.utils.errors import (
    AddressOutOfRangeError,
    ConnectionLostError,
    FUNCTION_CODE_BLOCKS,
    ReconnectExhaustedError,
    RequestCancelledError,
    ResponseTimeoutError,
    SUPPORTED_FUNCTION_CODES,
    WRITE_FUNCTION_CODES,
    ZeroLengthRequestError,
    validate_address,
    validate_block,
    validate_function_code,
)
from modbus_sim.utils.logger import get_logger

logger = get_logger("modbus_simu")


class ModbusRequest(object):
    """A single Modbus read/write request with an optional client request id."""

    def __init__(self, function_code, address, count=1, values=None,
                 request_id=None):
        self.function_code = validate_function_code(function_code)
        self.block = FUNCTION_CODE_BLOCKS[self.function_code]
        self.address = int(address)
        if values is not None:
            values = list(values)
            count = len(values)
        self.values = values
        self.count = int(count)
        if self.count <= 0:
            raise ZeroLengthRequestError(
                "Request length %s is not positive" % self.count,
                function_code=self.function_code, address=self.address)
        self.request_id = request_id

    @property
    def is_write(self):
        return self.function_code in WRITE_FUNCTION_CODES

    def __repr__(self):
        return ("ModbusRequest(request_id=%r, fc=%s, block=%s, address=%s, "
                "count=%s)" % (self.request_id, self.function_code,
                               self.block, self.address, self.count))


class RegisterFile(object):
    """In-memory register blocks with strict bounds and atomic writes."""

    def __init__(self):
        self._blocks = {}
        self._lock = threading.RLock()

    def add_block(self, name, start, size, default=0):
        validate_block(start, size)
        with self._lock:
            self._blocks[name] = (start, size, [default] * size)

    def remove_block(self, name):
        with self._lock:
            self._blocks.pop(name, None)

    def _get(self, name):
        try:
            return self._blocks[name]
        except KeyError:
            raise AddressOutOfRangeError(
                "No register block named %r" % name, block=name)

    def validate_write(self, name, address, values):
        with self._lock:
            start, size, _ = self._get(name)
            validate_address(address, len(values), start, size)

    def read(self, name, address, count):
        with self._lock:
            start, size, values = self._get(name)
            validate_address(address, count, start, size)
            offset = address - start
            return tuple(values[offset:offset + count])

    def write(self, name, address, values):
        """Validate the whole range, then apply atomically under the lock."""
        values = list(values)
        with self._lock:
            start, size, current = self._get(name)
            validate_address(address, len(values), start, size)
            offset = address - start
            current[offset:offset + len(values)] = values
        return len(values)

    def write_element(self, name, address, value):
        """Register-by-register write; only used by the legacy racy path."""
        with self._lock:
            start, size, current = self._get(name)
            validate_address(address, 1, start, size)
            current[address - start] = value

    def snapshot(self):
        with self._lock:
            return {name: (start, size, list(values))
                    for name, (start, size, values) in self._blocks.items()}


class VirtualConnection(object):
    """
    In-memory Modbus transport with deterministic fault injection.

    Knobs:
      refuse_connects: next N ``connect`` calls raise ConnectionLostError
      latency:         artificial delay for every request (seconds)
      transactional:   when True (default), writes validate fully and commit
                       atomically; when False, writes are applied register by
                       register (legacy behaviour kept to reproduce races)
      on_commit:       callable(self) invoked after a write is staged but
                       before it is committed; tests use it to disconnect at
                       exactly that instant
    """

    def __init__(self, register_file=None, latency=0.0, transactional=True):
        self.registers = register_file or RegisterFile()
        self.latency = latency
        self.transactional = transactional
        self.refuse_connects = 0
        self.on_commit = None
        self.connected = False
        self.connect_attempts = 0
        self.applied_requests = []
        self._lock = threading.RLock()

    def connect(self):
        self.connect_attempts += 1
        if self.refuse_connects > 0:
            self.refuse_connects -= 1
            raise ConnectionLostError(
                "Virtual connection refused on connect attempt %s"
                % self.connect_attempts,
                attempt=self.connect_attempts)
        self.connected = True

    def disconnect(self):
        self.connected = False

    def execute(self, request, cancel=None):
        if not self.connected:
            raise ConnectionLostError("Request issued on a closed connection")
        self._sleep(self.latency, cancel)
        if request.is_write:
            result = self._write(request, cancel)
        else:
            result = self.registers.read(
                request.block, request.address, request.count)
        with self._lock:
            self.applied_requests.append(request.request_id)
        return result

    def _write(self, request, cancel):
        if self.transactional:
            # Stage: validate the complete range without mutating anything.
            self.registers.validate_write(
                request.block, request.address, request.values)
            if self.on_commit is not None:
                self.on_commit(self)
            if cancel is not None and cancel.is_set():
                raise RequestCancelledError(
                    "Write cancelled before commit", request_id=request.request_id)
            if not self.connected:
                raise ConnectionLostError(
                    "Connection lost before write commit")
            return self.registers.write(
                request.block, request.address, request.values)
        # Legacy racy path: mutate register by register.
        for offset, value in enumerate(request.values):
            if not self.connected:
                raise ConnectionLostError(
                    "Connection lost in the middle of a write")
            self.registers.write_element(
                request.block, request.address + offset, value)
            if self.on_commit is not None:
                self.on_commit(self)
        return len(request.values)

    @staticmethod
    def _sleep(seconds, cancel):
        if not seconds:
            return
        deadline = time.time() + seconds
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            wait_for = min(remaining, 0.01)
            if cancel is not None:
                if cancel.wait(wait_for):
                    raise RequestCancelledError("Request cancelled while waiting")
            else:
                time.sleep(wait_for)


class ConnectionManager(object):
    """
    Execute requests over a connection with the unified lifecycle policy:

      * connection lost      -> disconnect, bounded reconnect + retry with
                                exponential backoff; after max_retries the
                                caller gets ReconnectExhaustedError while the
                                register file is left untouched (the next
                                call re-attempts recovery)
      * response timeout     -> ResponseTimeoutError immediately; the worker
                                is cancelled before commit, so a late response
                                cannot mutate state
      * validation errors    -> reported without retry, state untouched
      * repeated request_id  -> cached result returned, handler never rerun
    """

    def __init__(self, connection, max_retries=3, backoff=0.05, timeout=1.0,
                 dedup_size=128, sleeper=time.sleep):
        self.connection = connection
        self.max_retries = max_retries
        self.backoff = backoff
        self.timeout = timeout
        self.dedup_size = dedup_size
        self._sleeper = sleeper
        self._completed = OrderedDict()
        self._dedup_lock = threading.RLock()

    def execute(self, request):
        if request.request_id is not None:
            with self._dedup_lock:
                cached = self._completed.get(request.request_id)
            if cached is not None:
                logger.info("Repeated request %s ignored, returning cached "
                            "result", request.request_id)
                return cached
        result = self._execute_with_retry(request)
        if request.request_id is not None:
            with self._dedup_lock:
                self._completed[request.request_id] = result
                while len(self._completed) > self.dedup_size:
                    self._completed.popitem(last=False)
        return result

    def _execute_with_retry(self, request):
        attempts = 0
        while True:
            try:
                if not self.connection.connected:
                    self.connection.connect()
                return self._call_with_timeout(request)
            except ConnectionLostError as error:
                attempts += 1
                self.connection.disconnect()
                if attempts > self.max_retries:
                    raise ReconnectExhaustedError(
                        "Giving up on request %r after %s connection "
                        "attempts: %s" % (
                            request.request_id, attempts, error),
                        attempts=attempts, request_id=request.request_id)
                logger.warning("Connection lost (attempt %s/%s) for request "
                               "%r, retrying: %s", attempts,
                               self.max_retries + 1, request.request_id, error)
                if self.backoff:
                    self._sleeper(self.backoff * (2 ** (attempts - 1)))

    def _call_with_timeout(self, request):
        cancel = threading.Event()
        done = threading.Event()
        box = {}

        def worker():
            try:
                box["result"] = self.connection.execute(request, cancel=cancel)
            except RequestCancelledError:
                box["cancelled"] = True
            except Exception as error:
                box["error"] = error
            finally:
                done.set()

        thread = threading.Thread(target=worker,
                                  name="modbus-request-%s" % request.request_id)
        thread.daemon = True
        thread.start()
        if not done.wait(self.timeout):
            cancel.set()
            raise ResponseTimeoutError(
                "Request %r timed out after %s seconds" % (
                    request.request_id, self.timeout),
                request_id=request.request_id, timeout=self.timeout)
        if "error" in box:
            raise box["error"]
        return box["result"]
