import pytest

from agent import clock, restate
from agent.facts import FactStore


def agreed(now="2026-11-19T08:00"):
    f = FactStore.for_task(clock.parse_now(now))
    for k in ("time", "date", "party_size"):
        f.confirm(k)
    return f


def test_needs_all_four_confirmed():
    f = FactStore.for_task(clock.parse_now("2026-11-19T08:00"))
    f.set("restaurant_name", "The Crown & Anchor", "manager", 1, confirmed=True)
    assert restate.build(f) is None  # the manager has not agreed yet
    assert set(restate.missing(f)) == {"time", "date", "party_size"}


def test_full_restatement_london_date():
    f = agreed("2026-11-19T23:30")  # Prague is already on the 20th
    f.set("restaurant_name", "The Crown & Anchor", "manager", 1, confirmed=True)
    text = restate.build(f)
    assert "17:00" in text and "Thursday 19 November" in text and "for two" in text
    assert "The Crown & Anchor" in text
    assert restate.verify(text, f) == []


def test_restaurant_from_owner_is_not_enough():
    f = agreed()
    f.set("restaurant_name", "Dishoom", "owner", 1)  # not confirmed by the manager
    assert restate.build(f) is None


def test_name_and_condition_only_when_known():
    f = agreed()
    f.set("restaurant_name", "The Crown & Anchor", "manager", 1, confirmed=True)
    assert "under the name" not in restate.build(f)
    f.set("guest_name", "Yaroslav", "owner", 2)
    f.set("condition", "table back by 18:30", "manager", 3)
    text = restate.build(f)
    assert "under the name Yaroslav" in text and "table back by 18:30" in text


@pytest.mark.parametrize("variant", [0, 1, 2])
def test_variants_verify(variant):
    f = agreed()
    f.set("restaurant_name", "The Crown & Anchor", "manager", 1, confirmed=True)
    assert restate.verify(restate.build(f, variant=variant), f) == []


def test_verify_catches_a_wrong_time():
    f = agreed()
    f.set("restaurant_name", "The Crown & Anchor", "manager", 1, confirmed=True)
    bad = restate.build(f).replace("17:00", "18:30")
    assert restate.verify(bad, f)


@pytest.mark.parametrize("answer,time", [("да, 18:30 ок", "18:30"), ("ok", "18:30"), ("нет", "17:00"),
                                         ("Только 17:00", "17:00"), ("18:30", "18:30")])
def test_owner_free_text_on_another_time(answer, time):
    from agent import policy
    f = FactStore.for_task(clock.parse_now("2026-11-19T08:00"))
    f.set("offered_time", "18:30", "manager", 1)
    policy.apply_owner_answer(f, "alternative_time", answer, None, False, 2)
    assert f.get("time") == time
