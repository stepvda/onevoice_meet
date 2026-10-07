"""Did the meeting agree? The deterministic gate on AI adoptions.

Replaying a real meeting showed the language model marking decisions adopted
on an opinion ("my view of it is …") and while an agenda point was still
being introduced. So the model may only mark a decision adopted when the
transcript lines it cites show agreement, sentence by sentence:

  - a declared outcome, from anyone (the chair announcing it included):
    "agreed", "unanimously approved", "we all agree", "carried", "no
    objection", "the resolution is", "decided" …;
  - a response that answers someone else in the cited exchange: "that's a
    good idea", "let's go with that", "that's okay", "I like that", "I
    agree", or a reply that is nothing but assent ("Yes.", "To all? Yeah.").
    The same words inside one person's monologue are not consent ("whichever
    they choose, let's go with the other one").

Phrases that are negated ("I don't agree") or asked ("all in favour?") do
not count. Otherwise the decision stays proposed; a later tick that cites
the agreeing line adopts it.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

_DECLARED = re.compile(
    r"""\b(?:
        agreed|approved|adopted|carried|unanimous(?:ly)?|unanimity|consensus|seconded|accepted
      | (?:we|all|both|everyone|everybody)\ (?:\w+\ )?(?:agree|approve|accept|concur)s?
      | in\ agreement|(?:have|reached|got)\ (?:an?\ )?agreement
      | motion\ (?:passes|passed|carries)
      | (?:no|without)\ (?:objections?|opposition)
      | resolved|(?:the|our)\ resolution\ (?:is|on)
      | decided|(?:the|our)\ decision\ is
      | so\ be\ it|(?:it's|that's)\ settled
      | sign(?:ed)?\ off\ on
    )\b""",
    re.I | re.X,
)
_RESPONSE = re.compile(
    r"""\b(?:
        i\ (?:\w+\ ){0,2}(?:agree|approve|accept|concur)
      | in\ favou?r|i\ second
      | (?:we'll|we\ will|let's|let\ us)\ (?:go\ (?:with|ahead)|do\ (?:it|that|this)|proceed)
      | sounds\ good|that\ works|works\ for\ (?:me|us)|fine\ (?:with|by)\ (?:me|us)
      | (?:it's|that's)\ (?:a\ deal|fine|okay|ok)
      | (?:a\ )?(?:good|great)\ idea
      | i\ (?:\w+\ ){0,2}(?:like|love)\ (?:that|this|it|the\ idea)
      | happy\ with\ (?:that|it|this)
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

# A reply is plain assent when it holds one CORE word and, apart from fillers
# and SUPPORT words, at most one other word — and no negation ("Not sure.").
_CORE = {
    "yes", "yeah", "yep", "yup", "aye", "ok", "okay", "sure", "exactly", "absolutely",
    "definitely", "certainly", "indeed", "agreed", "perfect", "correct", "alright", "totally",
    "great", "fine",
}
_SUPPORT = {"good", "right", "all", "of", "course", "both", "me", "too", "for", "it", "that", "that's", "very", "sounds"}
_FILLER = {"um", "uh", "er", "erm", "hmm", "mm", "mhm", "so", "well", "oh", "and", "then"}
_NEGATIONS = {"no", "not", "nope", "never", "don't", "nah"}
ASSENT_WORDS = 6


def _statements(text: str) -> list[str]:
    """The sentences of a line that are not questions."""
    return [s.strip() for s in _SENTENCE.findall(text or "") if s.strip() and not s.strip().endswith("?")]


def _says(pattern: re.Pattern, text: str) -> bool:
    for sentence in _statements(text):
        for m in pattern.finditer(sentence):
            if not _NEGATED.search(sentence[max(0, m.start() - 40):m.start()]):
                return True
    return False


def declared_agreement(text: str) -> bool:
    return _says(_DECLARED, text)


def response_agreement(text: str) -> bool:
    return _says(_RESPONSE, text)


def plain_assent(text: str) -> bool:
    """The whole reply, questions aside, is a few words of assent ("To all?
    Yeah."). An "okay" that opens a longer reply is how people take the
    floor, not consent."""
    words = [w for s in _statements(text) for w in _WORD.findall(s.lower()) if w not in _FILLER]
    if not words or len(words) > ASSENT_WORDS or not any(w in _CORE for w in words):
        return False
    if any(w in _NEGATIONS for w in words):
        return False
    return sum(1 for w in words if w not in _CORE and w not in _SUPPORT) <= 1


def any_agreement(lines: Iterable[tuple[str | None, str]]) -> bool:
    """`lines` are the cited transcript lines as (speaker, text), in transcript
    order. True when one of them shows agreement (see the module docstring)."""
    speakers: set[str | None] = set()
    for who, text in lines:
        if declared_agreement(text):
            return True
        # Answers someone: an earlier cited line is another speaker's (an
        # unknown speaker is given the benefit of the doubt).
        answers = bool(speakers - {who}) or (who is None and bool(speakers))
        if answers and (response_agreement(text) or plain_assent(text)):
            return True
        speakers.add(who)
    return False
