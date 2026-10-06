"""Whisper repetition-loop detector (FDD section 7.4, repetition guard).

A loop is the same word n-gram repeated back to back. For 3- to 6-word
n-grams, 4 consecutive copies count as a loop ("I'm going to say" x 4, as
observed with turbo on turn.witysk.org). Shorter n-grams need more copies so
that ordinary emphasis ("no, no, no, no") is not flagged.

When a loop is found the text is truncated just before the loop starts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: n-gram length -> minimum number of consecutive copies that make a loop
MIN_REPEATS: dict[int, int] = {1: 8, 2: 6, 3: 4, 4: 4, 5: 4, 6: 4}

_TOKEN = re.compile(r"\S+")
_STRIP = re.compile(r"[^\w']+", re.UNICODE)
_TRAILING = " \t\r\n,;:-–—"


@dataclass(frozen=True)
class Repetition:
    found: bool
    text: str                 # original text, or text truncated before the loop
    ngram: str | None = None  # the repeated n-gram (normalised)
    repeats: int = 0
    start_char: int | None = None


def detect_repetition(text: str, min_repeats: dict[int, int] | None = None) -> Repetition:
    rules = MIN_REPEATS if min_repeats is None else min_repeats
    spans: list[tuple[int, str]] = []  # (char offset, normalised word)
    for m in _TOKEN.finditer(text):
        word = _STRIP.sub("", m.group(0).lower())
        if word:
            spans.append((m.start(), word))
    words = [w for _, w in spans]
    total = len(words)

    best: tuple[int, int, int] | None = None  # (start index, n, repeats)
    for n, need in sorted(rules.items()):
        if n * need > total:
            continue
        for i in range(0, total - n * need + 1):
            if best is not None and i >= best[0]:
                break  # only the earliest loop matters
            gram = words[i:i + n]
            reps = 1
            j = i + n
            while j + n <= total and words[j:j + n] == gram:
                reps += 1
                j += n
            if reps >= need:
                best = (i, n, reps)
                break

    if best is None:
        return Repetition(found=False, text=text)
    i, n, reps = best
    start_char = spans[i][0]
    return Repetition(
        found=True,
        text=text[:start_char].rstrip(_TRAILING),
        ngram=" ".join(words[i:i + n]),
        repeats=reps,
        start_char=start_char,
    )
