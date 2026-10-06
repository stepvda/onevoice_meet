"""The agreement gate on AI adoptions (app/meetpp/agreement.py)."""
from __future__ import annotations

from app.meetpp.agreement import any_agreement, phrase_agreement, plain_assent


def test_agreement_phrases():
    assert phrase_agreement("Okay, so that's unanimously approved.")
    assert phrase_agreement("So the resolution on that is, we keep the old supplier.")
    assert phrase_agreement("I think we all agree on the tank size.")
    assert phrase_agreement("No objections? Then it's carried.")
    assert phrase_agreement("Let's go with the second quote.")
    assert phrase_agreement("I can order it next week. That's fine.")


def test_proposals_questions_and_negations_are_not_agreement():
    assert not phrase_agreement("First is approving the minutes of the last meeting.")
    assert not phrase_agreement("Will you both be in favour?")
    assert not phrase_agreement("I don't agree with that at all.")
    assert not phrase_agreement("We haven't decided anything yet.")
    assert not phrase_agreement("My view is that we should stop using the hall.")
    assert not phrase_agreement("I disagree.")


def test_plain_assent_is_a_whole_short_reply():
    assert plain_assent("Yes.")
    assert plain_assent("To all? Yeah.")
    assert plain_assent("Okay, great.")
    assert plain_assent("Yes, exactly. Okay, yeah.")
    # An "okay" that opens a longer turn is taking the floor, not consent.
    assert not plain_assent("Okay. I asked the contractor which one is better, I'll let you know.")
    assert not plain_assent("Yeah, it all worked fine, so I moved the backup to the new server.")


def test_assent_counts_from_someone_other_than_the_opener():
    proposal = ("ana", "Should we split the grant report into two documents?")
    assert any_agreement([proposal, ("ben", "Yeah.")])
    # The proposer's own "yeah" is a back-channel.
    assert not any_agreement([proposal, ("ana", "Yeah.")])
    # An opinion with no reply.
    assert not any_agreement([("ana", "My view is it's wrong to outsource the drafting."),
                              ("ana", "They're very good at certain things, but not this.")])
    # An explicit phrase counts from anyone, the chair included.
    assert any_agreement([proposal, ("ana", "Good, that's agreed then.")])
    assert not any_agreement([])
