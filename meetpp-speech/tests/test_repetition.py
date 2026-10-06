from meetpp_speech.repetition import detect_repetition


def test_clean_text_not_flagged():
    text = ("The board approved the budget. The board also approved the minutes, "
            "and the board thanked the treasurer for the board report.")
    r = detect_repetition(text)
    assert not r.found and r.text == text


def test_turbo_loop_from_turn_is_flagged_and_truncated():
    text = "So the next item is the budget and I'm going to say " + "I'm going to say " * 40
    r = detect_repetition(text)
    assert r.found
    assert r.text == "So the next item is the budget and"
    assert r.ngram == "i'm going to say"
    assert r.repeats >= 40


def test_exactly_four_copies_of_3_to_6_grams():
    for n in range(3, 7):
        gram = " ".join(f"w{i}" for i in range(n))
        assert detect_repetition("intro " + (gram + " ") * 4).found, n
        assert not detect_repetition("intro " + (gram + " ") * 3 + "outro").found, n


def test_punctuation_and_case_are_ignored():
    text = "We agreed. Thank you all very much. thank you all very much, Thank you all very much! THANK YOU ALL VERY MUCH."
    r = detect_repetition(text)
    assert r.found and r.text == "We agreed."


def test_short_emphasis_is_not_a_loop():
    assert not detect_repetition("No, no, no, no, that is not what we decided.").found
    assert not detect_repetition("Yes yes yes yes yes yes.").found  # 6 x 1-word < 8


def test_long_single_word_loop():
    r = detect_repetition("Okay " + "you " * 20)
    assert r.found and r.text == "Okay" and r.ngram == "you"


def test_non_consecutive_repeats_are_not_a_loop():
    text = " ".join(["the vote is open"] * 1 + ["now", "the vote is open", "again", "the vote is open",
                                                "and", "the vote is open"])
    assert not detect_repetition(text).found


def test_loop_at_start_gives_empty_text():
    r = detect_repetition("going to say " * 6)
    assert r.found and r.text == ""


def test_empty_text():
    r = detect_repetition("")
    assert not r.found and r.text == ""
