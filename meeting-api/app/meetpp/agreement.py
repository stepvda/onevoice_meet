"""Did the meeting agree? The deterministic gate on AI adoptions.

Replaying a real meeting showed the language model marking decisions adopted
on an opinion ("my view of it is …") and while an agenda point was still
being introduced. So the model may only mark a decision adopted when one of
the transcript lines it cites shows agreement, sentence by sentence:

  - an agreement phrase ("agreed", "unanimously approved", "we all agree",
    "no objection", "the resolution is", "let's go with that", "that's
    fine" …), not negated ("I don't agree") and not asked ("in favour?");
  - or a reply that is nothing but assent ("Yes.", "To all? Yeah.", "Okay,
    great.") from someone other than the speaker who opened the cited
    exchange: the proposer's own "yeah" is a back-channel, not consent.

Otherwise the decision stays proposed; a later tick that cites the agreeing
line adopts it.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

_PHRASE = re.compile(
    r"""\b(?:
        agreed|approved|adopted|carried|unanimous(?:ly)?|unanimity|consensus|seconded|accepted
      | (?:i|we|all|both|everyone|everybody|you)\ (?:\w+\ )?(?:agree|approve|accept|concur)
      | in\ agreement|(?:have|reached|got)\ (?:an?\ )?agreement
      | in\ favou?r
      | motion\ (?:passes|passed|carries)
      | (?:no|without)\ (?:objections?|opposition)
      | i\ second
      | resolved|(?:the|our)\ resolution\ (?:is|on)
      | decided|(?:the|our)\ decision\ is
      | (?:we'll|we\ will|let's|let\ us)\ (?:go\ (?:with|ahead)|do\ (?:it|that|this)|proceed)
      | sounds\ good|that\ works|works\ for\ (?:me|us)|fine\ (?:with|by)\ (?:me|us)
      | so\ be\ it|(?:it's|that's)\ (?:a\ deal|settled|fine|okay|ok)|deal
      | sign(?:ed)?\ off\ on
    )\b""",
    re.I | re.X,
)
# A negation up to three words before the phrase cancels it.
_NEGATED = re.compile(
    r"\b(?:not|no|never|nobody|don't|doesn't|didn't|haven't|hasn't|can't|cannot|won't|isn't|aren't|wasn't)\b"
    r"(?:\W+\w+){0,3}\W*$",
    re.I,
)
_SENTENCE = re.compile(r"[^.?!]+[.?!]*")
_WORD = re.compile(r"[a-z']+")

# A sentence is plain assent when it holds one CORE word and, apart from
# fillers and SUPPORT words, at most one other word.
_CORE = {
    "yes", "yeah", "yep", "yup", "aye", "ok", "okay", "sure", "exactly", "absolutely",
    "definitely", "certainly", "indeed", "agreed", "perfect", "correct", "alright", "totally",
    "great", "fine",
}
_SUPPORT = {"good", "right", "all", "of", "course", "both", "me", "too", "for", "it", "that", "that's", "very", "sounds"}
_FILLER = {"um", "uh", "er", "erm", "hmm", "mm", "mhm", "so", "well", "oh", "and", "then"}
ASSENT_WORDS = 6


def _statements(text: str) -> list[str]:
    """The sentences of a line that are not questions."""
    return [s.strip() for s in _SENTENCE.findall(text or "") if s.strip() and not s.strip().endswith("?")]


def phrase_agreement(text: str) -> bool:
    for sentence in _statements(text):
        for m in _PHRASE.finditer(sentence):
            if _NEGATED.search(sentence[max(0, m.start() - 40):m.start()]):
                continue
            return True
    return False


def plain_assent(text: str) -> bool:
    """The whole reply, questions aside, is a few words of assent ("To all?
    Yeah."). An "okay" that opens a longer reply is how people take the
    floor, not consent."""
    words = [w for s in _statements(text) for w in _WORD.findall(s.lower()) if w not in _FILLER]
    if not words or len(words) > ASSENT_WORDS or not any(w in _CORE for w in words):
        return False
    return sum(1 for w in words if w not in _CORE and w not in _SUPPORT) <= 1


def any_agreement(lines: Iterable[tuple[str | None, str]]) -> bool:
    """`lines` are the cited transcript lines as (speaker, text), in transcript
    order. True when one of them shows agreement (see the module docstring)."""
    lines = list(lines)
    opener = next((who for who, _ in lines if who), None)
    for who, text in lines:
        if phrase_agreement(text):
            return True
        if plain_assent(text) and (opener is None or who is None or who != opener):
            return True
    return False
