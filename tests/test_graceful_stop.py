"""A stop finishes the pass in hand.

systemctl stop, `up` shutting down and a closed terminal all reach the daemon
as a signal. Killed mid-pass, a turn's reap or a registry save is lost; so the
signal only raises a flag, the pass runs to its end (save included), and the
loop exits 0 at the top of the next pass. A stop that lands in the sleep
between passes ends it within a second.

No claude, no gh, no network: every collaborator of a pass is stubbed.

Run directly: python3 tests/test_graceful_stop.py
"""

import os
import signal
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline


class Cfg:
    def __init__(self, poll_interval):
        self.data_dir = Path(tempfile.mkdtemp())
        self.runner = {"poll_interval": poll_interval, "max_concurrent_runs": 1}
        self.intake = {"provider": "none"}


class Reg:
    saves = []

    def __init__(self, _path):
        self.data = {"stories": {}}

    def stories(self, *_a):
        return {}

    def save(self):
        Reg.saves.append(time.time())


@contextmanager
def stubbed_pass(during_reap=None):
    """Every collaborator of one daemon pass replaced; `during_reap` runs in
    the middle of the pass, between two registry saves' worth of work."""
    Reg.saves = []
    names = {
        "GitHub": lambda *_a: None,
        "_preflight_or_exit": lambda *_a: None,
        "_log_stale_nodes": lambda *_a: None,
        "Registry": Reg,
        "_consume_requests": lambda *_a: None,
        "_reap_tasks": lambda *_a: during_reap and during_reap(),
        "log": lambda *_a: None,
    }
    saved = {n: getattr(pipeline, n) for n in names}
    mods = [(pipeline.board_mod, "make_board", lambda *_a: None),
            (pipeline.status_mod, "install_page", lambda *_a: None),
            (pipeline.status_mod, "write_status", lambda *_a, **_k: None),
            (pipeline.surface_mod, "tick", lambda *_a: None),
            (pipeline.surface_mod, "reconcile_merged_holds", lambda *_a: None),
            (pipeline.engine_seam, "enabled", lambda: False),
            (pipeline.runs_mod, "all_active", lambda *_a: []),
            # `run` stamps its code into the status module; keep that local
            (pipeline.status_mod, "CODE", None)]
    saved_mods = [(m, n, getattr(m, n)) for m, n, _ in mods]
    handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    for n, v in names.items():
        setattr(pipeline, n, v)
    for m, n, v in mods:
        setattr(m, n, v)
    try:
        yield
    finally:
        for n, v in saved.items():
            setattr(pipeline, n, v)
        for m, n, v in saved_mods:
            setattr(m, n, v)
        for s, h in handlers.items():
            signal.signal(s, h)


def test_a_sigterm_mid_pass_lets_the_pass_save_then_exits_at_the_next_pass():
    signalled = []

    def term_mid_pass():
        if not signalled:
            signalled.append(len(Reg.saves))
            os.kill(os.getpid(), signal.SIGTERM)

    with stubbed_pass(during_reap=term_mid_pass):
        # a short interval: the loop reaches the top of the next pass at once
        pipeline.cmd_run(Cfg(poll_interval=0.01), None)
    # the reap's own save ran after the signal arrived, and no second pass did
    assert signalled == [0]
    assert len(Reg.saves) == 1, Reg.saves


def test_a_stop_in_the_sleep_between_passes_ends_it_within_a_second():
    with stubbed_pass():
        threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGHUP)).start()
        t0 = time.time()
        pipeline.cmd_run(Cfg(poll_interval=30), None)
        took = time.time() - t0
    assert took < 2, took
    assert len(Reg.saves) == 1


def test_up_treats_a_hangup_as_ctrl_c_and_stops_its_children():
    child = [("sleeper", [sys.executable, "-c", "import time; time.sleep(30)"])]
    handlers = signal.getsignal(signal.SIGHUP)
    log = pipeline.log
    pipeline.log = lambda *_a: None
    try:
        threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGHUP)).start()
        t0 = time.time()
        status = pipeline._supervise(child, grace=5)
    finally:
        pipeline.log = log
        signal.signal(signal.SIGHUP, handlers)
    assert status == 0, "a hangup is a stop, not a child dying on its own"
    assert time.time() - t0 < 5


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
