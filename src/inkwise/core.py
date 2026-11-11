"""Bounded text processing, evidence fusion and deterministic review gates."""

import math
import re
from collections import defaultdict
from dataclasses import dataclass

MAX_CHARACTERS = 15_000
DIMENSIONS = ("content", "coherence", "expression", "vocabulary", "grammar")


@dataclass(frozen=True)
class Chunk:
    """Non-overlapping core offsets with bounded read-only context."""

    start: int
    end: int
    context_start: int
    context_end: int


def split_text(text: str, limit: int = 800, overlap: int = 80) -> list[Chunk]:
    """Split code points without dropping content; production adds a tokenizer.

    Prefer paragraph and safe sentence endings. English periods are deliberately
    not treated as safe boundaries here, preserving abbreviations and decimals.
    Oversized sentences are hard-split at the character limit, never unbounded.
    """
    if not text.strip() or len(text) > MAX_CHARACTERS:
        raise ValueError("Text must contain 1–15000 non-empty code points")
    if limit < 1 or overlap < 0 or overlap >= limit:
        raise ValueError("Require 0 <= overlap < limit")
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            matches = list(re.finditer(r"[。！？!?\n][”’\"']?", text[start:end]))
            if matches and matches[-1].end() >= limit // 2:
                end = start + matches[-1].end()
        chunks.append(Chunk(start, end, max(0, start - overlap), min(len(text), end + overlap)))
        start = end
    return chunks


def fuse_ranks(routes: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Fuse canonical evidence IDs; repeated hits in a route get no bonus."""
    if k < 1:
        raise ValueError("k must be positive")
    scores: dict[str, float] = defaultdict(float)
    for route in routes:
        unique = dict.fromkeys(route)
        for rank, evidence_id in enumerate(unique, 1):
            scores[evidence_id] += 1 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def validate_quote(text: str, start: int, end: int, quote: str) -> bool:
    """Verify an exact non-empty quote against code point offsets."""
    return 0 <= start < end <= len(text) and text[start:end] == quote


def validate_scores(scores: dict[str, float]) -> None:
    """Reject partial, extra, non-finite or out-of-band grading results."""
    if set(scores) != set(DIMENSIONS):
        raise ValueError("All five dimensions are required")
    if any(type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 4
           for value in scores.values()):
        raise ValueError("Band must be finite and within 0–4")


def require_review(initial: dict[str, float], revised: dict[str, float], *,
                   rules_available: bool, ocr_confirmed: bool, evidence_valid: bool) -> bool:
    """Hard safety gates outrank model agreement or self-reported confidence."""
    validate_scores(initial)
    validate_scores(revised)
    return (not rules_available or not ocr_confirmed or not evidence_valid
            or any(abs(initial[d] - revised[d]) >= 1 for d in DIMENSIONS))
