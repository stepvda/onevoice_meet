"""The agreement gate on AI adoptions (app/meetpp/agreement.py)."""
from __future__ import annotations

from app.meetpp.agreement import any_agreement, declared_agreement, plain_assent, response_agreement


def test_declared_outcomes():
    assert declared_agreement("Okay, so that's unanimously approved.")
    assert declared_agreement("So the resolution on that is, we keep the old supplier.")
    assert declared_agreement("No objections? Then it's carried.")
    assert declared_agreement("Good, that's agreed then.")
    assert declared_agreement("I think we all agree on the tank size.")


def test_responses():
    assert response_agreement("Actually, that's a good idea.")
    assert response_agreement("I agree with Ben on the tank size.")
    assert response_agreement("Let's go with the second quote.")
    assert response_agreement("I can order it next week. That's okay.")
    assert response_agreement("I guess I kind of like that.")


def test_proposals_questions_and_negations_are_not_agreement():
    for text in (
        "First is approving the minutes of the last meeting.",
        "Will you both be in favour?",
        "I don't agree with that at all.",
        "We haven't decided anything yet.",
        "My view is that we should stop using the hall.",
        "I disagree.",
        "That's not a good idea.",
        "It depends on how we deal with the data.",
    ):
        assert not declared_agreement(text) and not response_agreement(text), text


def test_plain_assent_is_a_whole_short_reply():
    assert plain_assent("Yes.")
    assert plain_assent("To all? Yeah.")
    assert plain_assent("Okay, great.")
    assert plain_assent("Yes, exactly. Okay, yeah.")
    assert not plain_assent("Not sure.")
    assert not plain_assent("No, sure, but later.")
    # An "okay" that opens a longer turn is taking the floor, not consent.
    assert not plain_assent("Okay. I asked the contractor which one is better, I'll let you know.")
    assert not plain_assent("Yeah, it all worked fine, so I moved the backup to the new server.")


def test_a_response_must_answer_someone_else():
    proposal = ("ana", "Should we split the grant report into two documents?")
    assert any_agreement([proposal, ("ben", "Yeah.")])
    assert any_agreement([proposal, ("ben", "Or one report with two parts?"), ("ana", "Actually, that's a good idea.")])
    # The proposer's own "yeah", or a figure of speech in a monologue, is not consent.
    assert not any_agreement([proposal, ("ana", "Yeah.")])
    assert not any_agreement([("ana", "My view is it's wrong to outsource the drafting."),
                              ("ana", "Whichever supplier they pick, let's go with the other one.")])
    # A declared outcome counts from anyone, the chair included.
    assert any_agreement([proposal, ("ana", "Good, that's agreed then.")])
    assert not any_agreement([])


def test_the_cited_exchange_must_be_about_the_decision():
    from app.meetpp.agreement import about

    subject = "Keep backups of the latest models"
    assert about(subject, ["Should we keep a backup of the newest model?", "Yes."])
    # Agreement to something else: nothing about backups or models.
    assert not about(subject, ["Are we good with splitting the terms and the privacy policy?",
                               "I think it's a good idea, so let's try to do that."])
    assert about("", ["anything"])
