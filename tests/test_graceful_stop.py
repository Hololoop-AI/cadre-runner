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
import subprocess
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
        self.intake = {"provider": "none", "phase_states": {}}
        self.limits = {"max_rounds_per_stage": 5}


class Reg:
    saves = []

    tasks = {}
    polled = []
    story_set = {}

    def __init__(self, _path):
        self.data = {"stories": {}, "tasks": Reg.tasks}

    def stories(self, *a):
        return {} if a else dict(Reg.story_set)

    def save(self):
        Reg.saves.append(time.time())


@contextmanager
def stubbed_pass(during_reap=None):
    """Every collaborator of one daemon pass replaced; `during_reap` runs in
    the middle of the pass, between two registry saves' worth of work."""
    Reg.saves = []
    Reg.polled = []
    names = {
        "_poll_story": lambda _c, _r, _g, slug, _s: Reg.polled.append(slug),
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
            (pipeline.board_mod, "mirror_status", lambda *_a, **_k: None),
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


def _passes_while(cfg, poke_at, poke, stop_at):
    """Run the daemon with `poke` fired at `poke_at` s and a stop at
    `stop_at` s; return when each pass saved, relative to the start."""
    with stubbed_pass():
        if poke:
            threading.Timer(poke_at, poke).start()
        threading.Timer(stop_at, os.kill, (os.getpid(), signal.SIGHUP)).start()
        t0 = time.time()
        pipeline.cmd_run(cfg, None)
    return [round(s - t0, 2) for s in Reg.saves]


def test_a_ring_from_the_api_starts_a_pass_at_once_without_polling_github():
    cfg = Cfg(poll_interval=30)
    # from another process, as the API and the CLI do — the daemon's own
    # process never rings itself
    ring = [sys.executable, "-c",
            "import sys; from types import SimpleNamespace as N; sys.path.insert(0, %r);"
            " from runnerlib import wake; sys.exit(0 if wake.notify(N(data_dir=%r)) else 1)"
            % (str(Path(pipeline.__file__).parent), str(cfg.data_dir))]
    rang = []
    Reg.story_set = {"s": {}}  # one story, so a GitHub poll is visible
    try:
        saves = _passes_while(cfg, 0.3, lambda: rang.append(subprocess.call(ring)),
                              stop_at=3.5)
    finally:
        Reg.story_set = {}
    assert rang == [0], "the listener took the ring"
    # pass 1 saves twice (its story, its reap); the ring at ~0.3 s starts
    # pass 2 straight away, which saves once — and nothing else until 30 s
    assert len(saves) == 3, saves
    assert saves[2] < 0.8, saves
    # GitHub was polled by the timed first pass only, not the pushed one
    assert Reg.polled == ["s"], Reg.polled


def test_a_run_exiting_ends_the_wait():
    cfg = Cfg(poll_interval=30)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2.5)"])
    Reg.tasks = {"t": {"active_runs": {"r": {"pid": child.pid}}}}
    try:
        saves = _passes_while(cfg, 0, None, stop_at=4.5)
    finally:
        Reg.tasks = {}
        child.wait()
    # the child exits at ~2.5 s: one more pass then, not at 30 s
    assert len(saves) == 2, saves
    assert 2.4 <= saves[1] < 3.5, saves


def test_with_nothing_pushed_the_daemon_sleeps_the_full_interval():
    saves = _passes_while(Cfg(poll_interval=30), 0, None, stop_at=3.5)
    assert len(saves) == 1, saves


def test_ringing_a_daemon_that_is_not_running_is_harmless():
    assert pipeline.wake_mod.notify(Cfg(poll_interval=1), "task") is False


def test_the_daemon_does_not_ring_itself():
    cfg = Cfg(poll_interval=1)
    listener = pipeline.wake_mod.Listener(cfg)
    try:
        assert pipeline.wake_mod.notify(cfg, "from inside") is False
        assert listener.wait(0.1) == []
    finally:
        listener.close()
    assert not pipeline.wake_mod.sock_path(cfg).exists()


def test_an_api_thread_in_the_same_process_rings_the_loop():
    # `pipeline.py serve`: request threads share the loop's process; only the
    # loop's own thread is muted
    cfg = Cfg(poll_interval=1)
    listener = pipeline.wake_mod.Listener(cfg)
    try:
        took = []
        t = threading.Thread(target=lambda: took.append(pipeline.wake_mod.notify(cfg, "task")))
        t.start()
        t.join()
        assert took == [True]
        assert listener.wait(1.0) == ["task"]
    finally:
        listener.close()


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
