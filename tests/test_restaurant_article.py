from agent import clock, policy, restate
from agent.facts import FactStore


def test_the_kept_as_the_manager_wrote_it():
    assert policy.with_the(["No, I said the restaurant name is the Ivy."], "Ivy") == "the Ivy"
    assert policy.with_the(["Booked at The Wolseley, see you then"], "Wolseley") == "The Wolseley"
    assert policy.with_the(["The Ivy here."], "The Ivy") == "The Ivy"


def test_no_the_added_without_it():
    assert policy.with_the(["Our restaurant is Dishoom."], "Dishoom") == "Dishoom"
    assert policy.with_the(["You're booked at Rules. Please read the rules first."], "Rules") == "Rules"
    assert policy.with_the(["the Ivy Asia is next door, you are at Ivy"], "Iv") == "Iv"


def test_restatement_reads_at_the_ivy():
    f = FactStore.for_task(clock.parse_now("2026-11-19T08:00"))
    for k in ("time", "date", "party_size"):
        f.confirm(k)
    f.set("restaurant_name", "the Ivy", "manager", 1, confirmed=True)
    text = restate.build(f)
    assert "at the Ivy" in text and restate.verify(text, f) == []


def test_the_is_not_a_change_of_restaurant():
    f = FactStore.for_task(clock.parse_now("2026-11-19T08:00"))
    for k in ("time", "date", "party_size"):
        f.confirm(k)
    f.set("restaurant_name", "Ivy", "manager", 1, confirmed=True)
    before = restate.signature(f)
    f.set("restaurant_name", "the Ivy", "manager", 2, confirmed=True)
    assert restate.signature(f) == before
    assert not restate.is_correction(before, restate.signature(f))
    f.set("restaurant_name", "Apple", "manager", 3, confirmed=True)
    assert restate.is_correction(before, restate.signature(f))
