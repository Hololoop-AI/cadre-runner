"""The prompt-variable checks for dialogue turns. (A run's skills are
runnerlib/skillstore.py's.)"""

from pathlib import Path
from string import Template

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Names prompts used before a rename. A prompt version recorded before the
# rename still asks for them, and until someone promotes the new one the runner
# fills them under both names (engine_seam) — or says so when it cannot.
PROMPT_ALIASES = ("routes", "route_options")


def known_prompt_vars(prompts_dir=None) -> frozenset:
    """Every lowercase `$name` the shipped prompts ask for, plus the old aliases.

    Only these count as unfilled. A prompt also carries shell environment
    (`$CADRE_SURFACE_OUT`) that `safe_substitute` is right to leave alone, so
    "any `$` left over" would cry wolf on every spawn."""
    d = Path(prompts_dir or PROMPTS_DIR)
    names = set(PROMPT_ALIASES)
    for f in d.glob("*.md"):
        names |= {n for n in _identifiers(f.read_text()) if n.islower()}
    return frozenset(names)


def _identifiers(template: str) -> set:
    return {m.group("named") or m.group("braced")
            for m in Template.pattern.finditer(template)
            if m.group("named") or m.group("braced")}


def unfilled(template: str, variables: dict, known=None) -> list[str]:
    """The known prompt variables `template` asks for that `variables` does
    not supply — each one reaches the session as a literal `$name`.

    Read from the template, not the rendered text, so a driver's note that
    happens to quote `$feedback` is not mistaken for a hole."""
    known = known_prompt_vars() if known is None else known
    return sorted(n for n in _identifiers(template)
                  if n in known and n not in variables)


