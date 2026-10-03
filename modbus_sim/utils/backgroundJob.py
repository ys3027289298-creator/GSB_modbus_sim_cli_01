"""
Copyright (c) 2016 Riptide IO, Inc. All Rights Reserved.

"""

import threading
import logging


class BackgroundJob(threading.Thread):
    def __init__(self, name, interval, function):
        threading.Thread.__init__(self)
        self._name = name
        self._logger = logging.getLogger("modbus_tk")
        self.interval = interval
        self.simulate_func = function
        self.stop_timer = threading.Event()
        self.daemon = True

    def run(self):
        self._logger.info("Start %s thread" % self._name)
        while not self.stop_timer.is_set():
            if not self.stop_timer.is_set():
                try:
                    self.simulate_func()
                except Exception:
                    # A single failing tick (e.g. response timeout, lost
                    # connection) must never kill the scheduled job.
                    self._logger.exception(
                        "%s tick failed, schedule continues", self._name)
            self.stop_timer.wait(self.interval)
        self._logger.info("Stop %s thread" % self._name)

    def cancel(self, timeout=None):
        self.stop_timer.set()
        # Wait for a tick that is already running instead of returning while
        # the job still mutates state (cancel/start race).
        if self.is_alive():
            self.join(timeout)

