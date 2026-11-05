"""Push into the daemon: the doorbell between the backend API and the loop.

The daemon used to learn about new work by sleeping `poll_interval` and then
re-reading everything. Measured over 107 tasks that sleep was most of the wait
between asking and the session starting (median 8 s, up to 15 s; `claude -p`
itself is ready in ~0.3 s). Now whoever hands the daemon work rings it:

- `notify(cfg)` — a datagram on `<data_dir>/daemon.sock`. The task API (in
  the same process under `pipeline.py serve`, or its own), the CLI and the
  surface proxy call it right after the work is durable on the
  board / in the page server, so the message carries no payload that matters:
  it says "look now", and the board stays the one source of truth. A daemon
  that is down misses nothing; it reads the board when it comes up.
- a session ending — `pidfd_open` on each run's wrapper pid, so the kernel
  says when a turn finished instead of the daemon checking for its exit file.
  Linux only; elsewhere (macOS) the timed pass still reaps.
- a stop signal — `signal.set_wakeup_fd`, so systemctl stop ends the wait at
  once rather than at the next 1 s step.

A pushed pass runs at once and does only the local work (the engine, task
turns, page feedback): about 0.1 s. The timed pass every `poll_interval` adds
what cannot push — the legacy pull-request pipeline's GitHub polling and
tracker intake, both of which have nothing to do unless a story or a tracker
is configured — and enforces run deadlines.
"""

import os
import select
import signal
import socket
import threading
from pathlib import Path

# The thread running the engine loop. Its writes go through the same functions
# as everyone else's, and ringing itself would start a pass that writes, rings,
# and starts another. Only that thread is muted: under `pipeline.py serve` the
# API's request threads live in the same process, and their rings are real.
_LOOP_THREAD = None


def sock_path(cfg) -> Path:
    return Path(cfg.data_dir) / "daemon.sock"


def notify(cfg, reason: str = "work") -> bool:
    """Ring the daemon. Never raises and never blocks: the work is already
    recorded, so a daemon that is not listening just finds it later. True when
    a listener took the message."""
    if threading.get_ident() == _LOOP_THREAD:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.setblocking(False)
            s.sendto(reason.encode()[:256], str(sock_path(cfg)))
        return True
    except OSError:
        return False


class Listener:
    """The daemon's end. `wait` blocks until something rings, a watched run
    exits, a signal lands, or the timeout passes."""

    def __init__(self, cfg):
        global _LOOP_THREAD
        path = sock_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)  # a previous daemon's, left by a crash
        self.path = path
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(str(path))
        self.sock.setblocking(False)
        self._sig_r, sig_w = os.pipe()
        os.set_blocking(self._sig_r, False)
        os.set_blocking(sig_w, False)
        self._sig_w = sig_w
        self._prev_wakeup = signal.set_wakeup_fd(sig_w)
        self._pidfds: dict[int, int] = {}
        # pids already reported as exited: a run the pass did not collect
        # yet must not ring again on every wait and spin the loop
        self._reported: set[int] = set()
        _LOOP_THREAD = threading.get_ident()

    def watch(self, pids) -> None:
        """Hold a pidfd for each pid in `pids`, drop the rest. A pid that is
        already gone gets none — the pass that follows reaps it anyway."""
        pids = set(pids)
        self._reported &= pids
        for pid in list(self._pidfds):
            if pid not in pids:
                os.close(self._pidfds.pop(pid))
        if not hasattr(os, "pidfd_open"):
            return
        for pid in pids - set(self._pidfds) - self._reported:
            try:
                self._pidfds[pid] = os.pidfd_open(pid)
            except OSError:
                pass

    def drain(self) -> list[str]:
        """Everything that rang since the last call, signal bytes included."""
        got = []
        while True:
            try:
                got.append(self.sock.recv(256).decode(errors="replace"))
            except (BlockingIOError, InterruptedError):
                break
        try:
            while os.read(self._sig_r, 512):
                got.append("signal")
        except (BlockingIOError, InterruptedError):
            pass
        return got

    def wait(self, timeout: float) -> list[str]:
        """Why the wait ended: rings and signals by name, `exit:<pid>` for a
        watched run that finished; empty on timeout."""
        fds = [self.sock, self._sig_r, *self._pidfds.values()]
        ready, _, _ = select.select(fds, [], [], max(0.0, timeout))
        why = self.drain() if (self.sock in ready or self._sig_r in ready) else []
        for pid, fd in list(self._pidfds.items()):
            if fd in ready:
                why.append(f"exit:{pid}")
                os.close(self._pidfds.pop(pid))
                self._reported.add(pid)
        return why

    def close(self) -> None:
        global _LOOP_THREAD
        _LOOP_THREAD = None
        signal.set_wakeup_fd(self._prev_wakeup)
        for fd in self._pidfds.values():
            os.close(fd)
        self._pidfds.clear()
        os.close(self._sig_r)
        os.close(self._sig_w)
        self.sock.close()
        self.path.unlink(missing_ok=True)
