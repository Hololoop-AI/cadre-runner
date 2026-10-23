"""py-fsrs 6.3.2 (MIT, open-spaced-repetition), the scheduler only.

Vendored so cadre-runner keeps its stdlib-only install: the optimizer (which
needs torch) is left out, `typing_extensions.Self` became `typing.Self`
(Python 3.11+), and the package-absolute imports became relative. Nothing
else is changed — upgrade by re-copying the same five files from a newer wheel.
The same algorithm as Arboreus's rs-fsrs, so the card state carries across.
"""

from .card import Card
from .rating import Rating
from .review_log import ReviewLog
from .scheduler import Scheduler
from .state import State

__all__ = ["Card", "Rating", "ReviewLog", "Scheduler", "State"]
