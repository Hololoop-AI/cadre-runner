"""Driver messages: asking never blocks, waiting is explicit, and the exit
codes let a shell script branch without parsing prose."""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib import messages as M

with tempfile.TemporaryDirectory() as td:
    d = Path(td)

    # asking returns immediately with a ticket; nothing is answered yet
    t0 = time.time()
    tk = M.ask(d, "nex-1", "Two shapes fit here — which do you want?",
               recommendation="the flatter one; it reads better in review",
               stage="build", slice_name="s1", pr=42)
    assert time.time() - t0 < 1.0, "ask must not block"
    m = M.get(d, tk)
    assert m["answer"] is None and m["story"] == "nex-1" and m["pr"] == 42

    # a check with no wait returns straight away, still unanswered
    t0 = time.time()
    m = M.wait(d, tk, timeout=0)
    assert m["answer"] is None and time.time() - t0 < 1.0

    # pending lists it; answering clears it from the list
    assert [x["ticket"] for x in M.pending(d)] == [tk]
    assert [x["ticket"] for x in M.pending(d, story="other")] == []
    M.answer(d, tk, "the flatter one")
    assert M.pending(d) == []
    assert M.get(d, tk)["answer"] == "the flatter one"

    # an already-answered ticket returns instantly rather than burning the window
    t0 = time.time()
    m = M.wait(d, tk, timeout=30)
    assert m["answer"] == "the flatter one" and time.time() - t0 < 1.0

    # the driver's last word wins
    M.answer(d, tk, "actually the other one")
    assert M.get(d, tk)["answer"] == "actually the other one"

    # unknown tickets are None, never an exception
    assert M.get(d, "nope") is None and M.answer(d, "nope", "x") is None

    # ordering: pending oldest-first, recent newest-first
    t_a = M.ask(d, "nex-2", "first")
    time.sleep(0.01)
    t_b = M.ask(d, "nex-2", "second")
    assert [x["ticket"] for x in M.pending(d, "nex-2")] == [t_a, t_b]
    assert [x["ticket"] for x in M.recent(d, limit=2)] == [t_b, t_a]

    # -- the CLI contract agents actually use --------------------------------
    root = Path(__file__).resolve().parent.parent
    cfg = d / "config.toml"
    cfg.write_text(f'[runner]\ndata_dir = "{d}"\n\n[limits]\nallowed_actors = ["driver"]\n')
    run = lambda *a: subprocess.run([sys.executable, str(root / "pipeline.py"),
                                     "--config", str(cfg), *a],
                                    capture_output=True, text=True)

    r = run("ask", "--story", "nex-3", "--question", "ship it?")
    assert r.returncode == 0, r.stderr
    ticket = r.stdout.strip()

    r = run("wait", "--ticket", ticket, "--timeout", "0")
    assert r.returncode == 2 and r.stdout.strip() == "pending"   # 2 = still open

    assert run("answer", "--ticket", ticket, "--text", "yes, ship").returncode == 0
    r = run("wait", "--ticket", ticket, "--timeout", "0")
    assert r.returncode == 0 and r.stdout.strip() == "yes, ship"

    assert run("wait", "--ticket", "bogus", "--timeout", "0").returncode == 3  # 3 = no ticket

print("messages tests: all passed")
