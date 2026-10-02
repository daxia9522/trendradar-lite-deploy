"""Minimal pollable child double; never executes an external command."""
import signal
import subprocess


class FakeProcess:
    def __init__(self, args=(), returncode=0):
        self.args = args
        self.returncode = returncode
        self.signals = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode

    def send_signal(self, signum):
        self.signals.append(signum)
        self.returncode = -signum

    def kill(self):
        self.send_signal(signal.SIGKILL)
