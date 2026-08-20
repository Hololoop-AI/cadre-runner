#!/usr/bin/env python3
"""CLI over scoring.score_spec. Usage: score_spec.py <result.json> <expected.json>"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scoring import score_spec, passed

checks = score_spec(json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))["claims"])
for name, ok, detail in checks:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))
print(f"\n{sum(1 for _, ok, _ in checks if ok)}/{len(checks)} claims hold")
sys.exit(0 if passed(checks) else 1)
