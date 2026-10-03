# Modbus TCP/RTU device simulation tool

# Install
```
$ python setup.py install
```

# Usage

```
$ modbus_simulator -h
usage: Modbus Simulator [-h] -c str
                        [--console-log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}]
                        [--enable-file-logging]
                        [--file-log-level {debug,info,warning,error,critical}]
                        [--log-file LOG_FILE] [--version] [-D]

- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

modbus_simu
~~~~~~~~~~

Modbus simulator CLI version based on Modbus tk

optional arguments:
  -h, --help            show this help message and exit
  -c (str), --simu-config (str)
                        Default configuration file to be used for modbus simulation script
  --console-log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}
                        Monitor console log level, overides value from configuration file
  --enable-file-logging
                        Enable file logging
  --file-log-level {debug,info,warning,error,critical}
                        Simulator file log level, overides value from configuration file
  --log-file LOG_FILE   Default simulation log file
  --version             show program's version number and exit
  -D, --debug           Turn on to enable tracing

- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
```

# To run
modbus_simulator  -c <PATH_TO_Config_File>

# sample simulation config
[conf.yml](modbus_sim/configs/conf.yml)

# Error handling and reconnection policy

All boundary violations are raised as subclasses of
`modbus_sim.utils.errors.ModbusSimError`, one family per boundary:

* register address space: `AddressOutOfRangeError`, `ZeroLengthRequestError`
* function codes: `UnsupportedFunctionCodeError`
* connection lifecycle: `ConnectionLostError`, `ReconnectExhaustedError`
* scheduled tasks: `ResponseTimeoutError` (a failing tick is logged and the
  schedule continues; `BackgroundJob.cancel()` joins the running tick)

When the connection drops, `ConnectionManager` (see
`modbus_sim/simulation/connection.py`) applies a **retry, then report**
policy - it never degrades to stale data:

1. Retry: reconnect and re-execute, bounded by `max_retries` with
   exponential backoff. Register state lives in `RegisterFile`, independent
   of the transport, so a reconnect never loses or duplicates data.
2. Report: once retries are exhausted, `ReconnectExhaustedError` is raised.
   The register file is left intact and the next request re-attempts
   recovery.
3. A response timeout raises `ResponseTimeoutError` immediately (no
   automatic retry, since the outcome is ambiguous) and cancels the
   in-flight write before commit, so a late response cannot mutate state.

Idempotency: requests may carry a `request_id`; completed ids are cached
(bounded) and retransmits return the cached result without re-applying.
Writes are validated as a whole and committed atomically, so repeated or
interrupted requests never leave partial register state.

# Tests

```
python3 -m unittest discover -s tests -v
```
