"""Run from backend/:  python -m pytest tests"""

from datetime import datetime, time, timedelta

from matching.constraints import (
    Donation, Receiver, Storage, Volunteer, Window,
    valid_receivers, valid_volunteers, validate_assignment,
)

NOW = datetime(2026, 11, 13, 21, 0)   # Friday
FRI = 5
ALL = {Storage.AMBIENT, Storage.CHILLED, Storage.FROZEN}


def w(start="07:00", end="23:00", day=FRI):
    return [Window(day, time.fromisoformat(start), time.fromisoformat(end))]


def donation(storage=Storage.CHILLED, count=4, safe_hours=3.0, food_type="cooked",
             deadline_min=60, ready_min=0):
    return Donation("d", "donor", "test", food_type, count, storage,
                    NOW + timedelta(minutes=ready_min), NOW + timedelta(minutes=deadline_min),
                    NOW + timedelta(hours=safe_hours))


def receiver(**kw):
    base = dict(id="r", name="R", storage_types=ALL, windows=w(), available_capacity=20)
    base.update(kw)
    return Receiver(**base)


def volunteer(**kw):
    base = dict(id="v", label="Volunteer #1", vehicle_capacity=10, cooler_capacity=8,
                availability=w("20:00", "23:00"))
    base.update(kw)
    return Volunteer(**base)


def at(minutes):
    return NOW + timedelta(minutes=minutes)


def check_r(d, r, eta=30):
    return valid_receivers(d, [r], {r.id: at(eta)})


def check_v(d, v, to_donor=10, travel=20):
    return valid_volunteers(d, [v], {v.id: at(to_donor)}, timedelta(minutes=travel))


# ----- receivers -----

def test_frozen_food_needs_frozen_storage():
    ok, rej = check_r(donation(Storage.FROZEN), receiver(storage_types={Storage.AMBIENT, Storage.CHILLED}))
    assert ok == [] and "frozen" in rej[0].reason


def test_chilled_food_needs_chilled_storage():
    ok, _ = check_r(donation(), receiver(storage_types={Storage.AMBIENT}))
    assert ok == []


def test_outside_receiving_window_rejected():
    ok, rej = check_r(donation(), receiver(windows=w("09:00", "17:00")))
    assert ok == [] and "not receiving" in rej[0].reason


def test_no_window_on_that_weekday():
    ok, _ = check_r(donation(), receiver(windows=w(day=1)))   # Monday only
    assert ok == []


def test_split_windows_second_one_counts():
    r = receiver(windows=w("08:00", "14:00") + w("16:00", "22:00"))
    ok, _ = check_r(donation(), r)
    assert len(ok) == 1


def test_receiver_capacity_in_crates():
    ok, _ = check_r(donation(count=8), receiver(available_capacity=5))
    assert ok == []


def test_unknown_capacity_does_not_block():
    ok, _ = check_r(donation(count=8), receiver(available_capacity=None))
    assert len(ok) == 1


def test_food_type_restriction():
    ok, _ = check_r(donation(food_type="dairy"), receiver(accepted_food_types={"bakery"}))
    assert ok == []


def test_food_must_be_safe_on_arrival():
    ok, rej = check_r(donation(safe_hours=0.75), receiver(), eta=20)   # 30 min buffer
    assert ok == [] and "unsafe" in rej[0].reason


# ----- volunteers -----

def test_cold_food_needs_cooler():
    ok, rej = check_v(donation(), volunteer(cooler_capacity=0))
    assert ok == [] and "no cooler" in rej[0].reason


def test_cooler_too_small():
    ok, rej = check_v(donation(count=6), volunteer(cooler_capacity=4))
    assert ok == [] and "cooler too small" in rej[0].reason


def test_ambient_food_ignores_cooler():
    d = donation(Storage.AMBIENT, food_type="bakery", safe_hours=24)
    ok, _ = check_v(d, volunteer(cooler_capacity=0))
    assert len(ok) == 1


def test_vehicle_capacity():
    ok, _ = check_v(donation(Storage.AMBIENT, count=12), volunteer(vehicle_capacity=6))
    assert ok == []


def test_volunteer_must_be_free_for_whole_trip():
    ok, _ = check_v(donation(), volunteer(availability=w("21:00", "21:20")))
    assert ok == []


def test_pickup_deadline():
    ok, rej = check_v(donation(deadline_min=15), volunteer(), to_donor=25)
    assert ok == [] and "deadline" in rej[0].reason


def test_early_volunteer_waits_until_ready():
    # arrives 21:10, food ready 21:30, delivery 21:50: still fine
    ok, _ = check_v(donation(ready_min=30, deadline_min=60), volunteer())
    assert len(ok) == 1


# ----- guardrail + seed -----

def test_validator_catches_bad_agent_proposal():
    errors = validate_assignment(donation(), receiver(storage_types={Storage.AMBIENT}),
                                 volunteer(cooler_capacity=0), at(10), timedelta(minutes=20))
    assert len(errors) == 2


def test_seed_data_loads():
    from data import repo
    assert len(repo.get_receivers()) == 6
    assert len(repo.get_donors()) == 10
    assert len(repo.get_volunteers()) == 8
    assert all(v.label.startswith("Volunteer #") for v in repo.get_volunteers())
    assert len(repo.get_donations()) == 5


def test_seed_catering_scenario():
    """The demo story: only Downtown Shelter can take the 9pm catering trays."""
    from data import repo
    d = next(x for x in repo.get_donations() if x.food_type == "cooked")
    receivers = repo.get_receivers()
    ok, _ = valid_receivers(d, receivers, {r.id: d.ready_at + timedelta(minutes=35) for r in receivers})
    assert [r.name for r in ok] == ["Downtown Shelter"]