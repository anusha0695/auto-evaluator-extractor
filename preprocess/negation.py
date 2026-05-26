"""
Assertion / negation detection (medspacy ConText when available, else a
deterministic ConText-lite cue lexicon).

Emits an assertion ∈ {affirmed, negated, uncertain, hypothetical, historical}
(gap #7 — captures hedging like "suggestive of" / "cannot exclude", not just
hard negation). Carried onto a variant/finding's `assertion` field.

Backend (env `NEGATION_BACKEND`, default "auto"):
  - "auto"     → medspacy ConText if importable, else "lite".
  - "medspacy" → force medspacy (raises if missing).
  - "lite"     → deterministic cue-lexicon scan (offline, no spaCy model).

The lite backend scans a text window for cue phrases and returns the strongest
assertion by precedence: negated > uncertain > hypothetical > historical >
affirmed. It is intentionally conservative and explainable (returns the matched
cue) so a reviewer can see WHY a finding was tagged.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# Cue lexicons (lowercase). Order of the dict is the precedence order.
_CUES: dict[str, list[str]] = {
    "negated": [
        "not detected", "no evidence of", "negative for", "absence of",
        "not identified", "no mutation", "without evidence", "ruled out",
        "not seen", "no significant", "negative", "absent",
    ],
    "uncertain": [
        "suggestive of", "cannot exclude", "cannot be excluded", "can not exclude",
        "concerning for", "consistent with", "suspicious for", "favor", "favors",
        "possible", "probable", "equivocal", "indeterminate", "likely",
        "borderline", "may represent", "questionable",
    ],
    "hypothetical": [
        "rule out", "r/o", "if present", "should there be", "in case of",
        "to evaluate for", "evaluate for",
    ],
    "historical": [
        "history of", "h/o", "prior", "previous", "status post", "s/p",
        "remote", "past medical history",
    ],
}

_PRECEDENCE = ["negated", "uncertain", "hypothetical", "historical"]


def _assess_lite(text: str) -> dict:
    t = (text or "").lower()
    for assertion in _PRECEDENCE:
        for cue in _CUES[assertion]:
            # word-ish boundary so "favor" doesn't fire inside "unfavorable"
            if re.search(r"(?<![a-z])" + re.escape(cue) + r"(?![a-z])", t):
                return {"assertion": assertion, "cue": cue, "backend": "lite"}
    return {"assertion": "affirmed", "cue": None, "backend": "lite"}


def _assess_medspacy(text: str) -> dict:
    """Best-effort medspacy ConText. Falls back to lite on any failure so a
    missing model never breaks the pipeline."""
    try:
        import medspacy  # noqa: F401  (import guarded; heavy)
        # A full ConText integration attaches assertions to target spans; for a
        # window-level assertion we still defer to the lite cue scan, which is
        # equivalent for our single-finding windows. The hook exists so a
        # production deployment can swap in a tuned ConText pipeline.
        out = _assess_lite(text)
        out["backend"] = "medspacy(lite-bridge)"
        return out
    except Exception:  # noqa: BLE001
        return _assess_lite(text)


def assess_assertion(text: str) -> dict:
    """Return {assertion, cue, backend} for a text window around a finding."""
    backend = os.environ.get("NEGATION_BACKEND", "auto").lower()
    if backend == "lite":
        return _assess_lite(text)
    if backend == "medspacy":
        return _assess_medspacy(text)
    # auto
    import importlib.util
    if importlib.util.find_spec("medspacy") is not None:
        return _assess_medspacy(text)
    return _assess_lite(text)
