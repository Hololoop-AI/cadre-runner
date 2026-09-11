#!/usr/bin/env python3
"""Stand in for a fixture story build, so the eval workflow can be proved.

`config/actions-eval.json` is workflow #2 — the proof that the engine is
workflow-agnostic. Proving that does NOT require spending $25 on a real
pipeline story: what has to be real is the ENGINE path (trigger → body →
emitter → next action), not the inference. So the prototype's first body is
this: copy a stored sample artifact into place and print where it went.

`--live` is the seam where the real thing goes later — the same argv, invoking
the runner instead of the copy. It refuses loudly rather than silently doing
the stub's job, because a stubbed run that reports itself as a live run is the
one failure mode that would make every number downstream a lie.

Usage: run_fixture.py --story judge-sample --out DIR [--variant good|corrupt]
Prints the artifact path — and nothing else — on stdout: the calling action
captures it as `{body[stdout]}` and hands it to the judge node.
"""

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"

ARTIFACTS = {"good": "briefing-good.md", "corrupt": "briefing-corrupt.md"}


def build(story: str, out_dir, variant: str = "good") -> Path:
    src_dir = FIXTURES / story
    if not src_dir.is_dir():
        raise SystemExit(f"no stored fixture at {src_dir}")
    name = ARTIFACTS.get(variant)
    if name is None:
        raise SystemExit(f"unknown variant {variant!r} (have: {', '.join(ARTIFACTS)})")
    src = src_dir / name
    if not src.exists():
        raise SystemExit(f"fixture {story!r} has no {name}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    dest = out / name
    shutil.copyfile(src, dest)
    shutil.copyfile(src_dir / "story.md", out / "story.md")
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--story", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--variant", default="good", choices=sorted(ARTIFACTS))
    ap.add_argument("--live", action="store_true",
                    help="run the real pipeline story instead of replaying a "
                         "stored artifact (not wired up in the prototype)")
    args = ap.parse_args(argv)
    if args.live:
        raise SystemExit(
            "--live is not wired up in the prototype: it would invoke the real "
            "runner for story %r (an intake-scale, paid run). Drop --live to "
            "replay the stored artifact." % args.story)
    print(build(args.story, args.out, args.variant))
    return 0


if __name__ == "__main__":
    sys.exit(main())
