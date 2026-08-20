#!/usr/bin/env bash
# Run the spec-writer node against a fixture story and score it.
# Usage: run_spec_eval.sh <story-name> [model]
# Costs one intake-scale inference run — deliberate use only.
set -euo pipefail
cd "$(dirname "$0")"
STORY="${1:?story name, e.g. tag-notes}"
MODEL="${2:-sonnet}"   # ALWAYS pinned — never inherit the terminal's model
STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d /tmp/cadre-eval-XXXX)"
FIX="$WORK/repo"
python3 "fixtures/mini_notes.py" "$FIX" >/dev/null
NODES="${CADRE_NODES:-$HOME/Projects/cadre-workflows/workflows}"
PROMPT="Invoke the Workflow tool with scriptPath \"$NODES/spec-writer.js\" and args {\"story\": <the full contents of the story file at $PWD/stories/$STORY.md>, \"repo\": \"$FIX\", \"variant\": \"change-spec\", \"nodesDir\": \"$NODES\"}. Read the story file first. When the workflow returns, print its ENTIRE result as raw JSON with no commentary."
claude -p "$PROMPT" --model "$MODEL" --effort high --output-format json \
  --dangerously-skip-permissions > "$WORK/session.json"
python3 - "$WORK/session.json" "$WORK/result.json" <<'PY'
import json, re, sys
payload = json.load(open(sys.argv[1]))
text = payload.get("result", "")
m = re.search(r"\{.*\}", text, re.S)
open(sys.argv[2], "w").write(m.group(0) if m else "{}")
PY
mkdir -p results
DUR=$SECONDS
python3 - "$WORK/result.json" "stories/$STORY.expected.json" "$STORY" "$MODEL" "$DUR" "$WORK/session.json" <<'PY' | tee "results/$STAMP-$STORY.txt"
import json, sys
sys.path.insert(0, ".")
from scoring import score_spec, passed
from tracking import log_trial
result = json.load(open(sys.argv[1])); expected = json.load(open(sys.argv[2]))["claims"]
checks = score_spec(result, expected)
cost = (json.load(open(sys.argv[6])).get("total_cost_usd") or 0)
rec = log_trial("spec-eval", sys.argv[3], sys.argv[4], checks,
                {"duration_s": float(sys.argv[5]), "cost_usd": cost,
                 "slice_count": len(result.get("slices") or [])})
for n, ok, d in checks:
    print(("PASS  " if ok else "FAIL  ") + n + ((" — " + d) if d and not ok else ""))
print(f"\n{'PASS' if rec['passed'] else 'FAIL'} · {sum(ok for _,ok,_ in checks)}/{len(checks)} claims · "
      f"{rec['metrics']['duration_s']:.0f}s · ${cost:.2f} · logged to signals+mlflow")
sys.exit(0 if rec["passed"] else 1)
PY
echo "work dir kept: $WORK"
