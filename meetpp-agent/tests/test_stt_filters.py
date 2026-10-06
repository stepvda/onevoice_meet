from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from agent.stt import (
    BLOCKLIST_PHRASES,
    STTEngine,
    blocklist_match,
    build_prompt,
    normalize,
    tail_text,
    whisper_should_drop,
)


def seg(text, ns=0.05, lp=-0.3, cr=1.2):
    return SimpleNamespace(text=text, no_speech_prob=ns, avg_logprob=lp, compression_ratio=cr)


def test_blocklist_has_at_least_30_phrases():
    assert len(BLOCKLIST_PHRASES) >= 30


@pytest.mark.parametrize(
    "text",
    [
        "Thank you for watching!",
        "thanks for watching",
        "Please subscribe.",
        "Subtitles by the Amara.org community",
        "Subtitles by John Smith",
        "you",
        "You.",
        "Thank you.",
        "THANK YOU",
        "[Music]",
        "(applause)",
        "Don't forget to like and subscribe!",
    ],
)
def test_blocklist_full_utterance_hits(text):
    assert blocklist_match(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "Thank you for watching the budget so closely, Maria.",
        "You know what, let's move on.",
        "Thank you very much, that settles item three.",
        "Please subscribe the new members to the mailing list.",
        "The subtitles by default should be on in the stream.",
        "Music licensing is the next agenda point.",
        "Yes.",
        "Okay.",
    ],
)
def test_blocklist_does_not_match_partial(text):
    assert blocklist_match(text) is None


def test_normalize():
    assert normalize("  Don’t   Forget!! ") == "dont forget"


def test_whisper_rule_is_and():
    assert whisper_should_drop(0.7, -1.2) is True
    assert whisper_should_drop(0.7, -0.5) is False  # confident text despite no_speech
    assert whisper_should_drop(0.3, -1.5) is False  # low logprob but speech
    assert whisper_should_drop(None, -2.0) is False


def test_postprocess_reasons():
    pp = STTEngine.postprocess
    assert pp([]).dropped == "no_speech"
    assert pp([seg("blah", ns=0.9, lp=-1.4)]).dropped == "no_speech"
    kept = pp([seg("The motion carries.", ns=0.9, lp=-0.4)])
    assert kept.dropped is None and kept.text == "The motion carries."
    assert pp([seg("  ...  ")]).dropped == "empty"
    assert pp([seg("Thank you.")]).dropped == "hallucination"
    two = pp([seg(" Item one is closed."), seg(" Now item two.")])
    assert two.text == "Item one is closed. Now item two."


def test_tail_text_word_boundary():
    t = "alpha beta gamma delta epsilon"
    assert tail_text(t, 12) == "epsilon"
    assert tail_text(t, 100) == t


def test_build_prompt():
    glossary = "Meeting of OneVoice. Participants: Maria Peeters, Jan. Topics: budget, Icator."
    transcript = "x" * 50 + " " + " ".join(f"word{i}" for i in range(100))
    p = build_prompt(glossary, transcript)
    assert p.startswith(glossary)
    tail = p[len(glossary) + 1 :]
    assert len(tail) <= 200 and tail.endswith("word99") and not tail.startswith("ord")
    assert build_prompt("", "") is None
    assert build_prompt("", "hello there") == "hello there"


class FakeModel:
    def __init__(self, name):
        self.name = name
        self.kwargs = None

    def transcribe(self, audio, **kwargs):
        self.kwargs = kwargs
        return iter([seg(" Hello board.")]), SimpleNamespace(language="en")


def test_engine_decoding_parameters_and_degrade():
    models = {}

    def factory(name):
        models[name] = FakeModel(name)
        return models[name]

    eng = STTEngine("small", "base", model_factory=factory)
    eng.load()
    assert eng.ready.is_set() and eng.loaded == ["small", "base"]
    res = eng.transcribe(np.zeros(16000, dtype=np.float32), "Meeting of X.", degraded=False)
    assert res.text == "Hello board." and res.model == "small"
    kw = models["small"].kwargs
    assert kw["language"] == "en"
    assert kw["beam_size"] == 1
    assert tuple(kw["temperature"]) == (0.0, 0.2, 0.4)
    assert kw["compression_ratio_threshold"] == 2.4
    assert kw["log_prob_threshold"] == -1.0
    assert kw["no_speech_threshold"] == 0.6
    assert kw["condition_on_previous_text"] is False
    assert kw["vad_filter"] is False
    assert kw["initial_prompt"] == "Meeting of X."
    assert kw["max_new_tokens"] == 22  # 1 s of audio
    assert eng.transcribe(np.zeros(8000, dtype=np.float32), None, degraded=True).model == "base"


def test_decode_budget_scales_with_audio_and_is_capped():
    from agent.stt import max_new_tokens

    assert max_new_tokens(0.0) == 16
    assert max_new_tokens(15.0) == 106
    assert max_new_tokens(120.0) == 200


def test_engine_primary_failure_falls_back_loudly(caplog):
    def factory(name):
        if name == "small":
            raise RuntimeError("no model")
        return FakeModel(name)

    eng = STTEngine("small", "base", model_factory=factory)
    eng.load()
    assert eng.ready.is_set() and eng.primary == "base" and eng.load_error
