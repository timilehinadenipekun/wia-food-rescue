"""
Constraint filter for food rescue matching.

Pure functions only: no database, no API calls, no AI.
Everything they need (donation, candidates, travel times) is passed in, so they're
easy to test and safe to call from the agent tools, the validator and the approve endpoint.

Field names follow the team DB schema (Organization, Volunteer, Volunteer_Availability,
Receiving_Window, Donation). Quantities are in crates.
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum


# ---------- Data shapes ----------

class Storage(str, Enum):
    """Shared vocabulary for Donation.storage_required and Organization.storage_types."""
    AMBIENT = "ambient"
    CHILLED = "chilled"
    FROZEN = "frozen"


@dataclass
class Window:
    """A row from Receiving_Window or Volunteer_Availability."""
    day: int          # Mon=1 ... Sun=7 (same as Python's datetime.isoweekday())
    start: time
    end: time


@dataclass
class Donation:
    id: str
    donor_id: str
    description: str
    food_type: str                 # produce, pantry, bakery, dairy, cooked, meat
    count: int                     # crates
    storage_required: Storage
    ready_at: datetime
    pickup_deadline: datetime
    safe_until: datetime | None = None   # deliver-by time for food safety (see notes)


@dataclass
class Receiver:
    """An Organization with role = 'receiver'."""
    id: str
    name: str
    storage_types: set[Storage]
    windows: list[Window]
    available_capacity: int | None = None     # crates; None = unknown, don't block
    accepted_food_types: set[str] = field(default_factory=set)  # empty = accepts all
    requires_confirmation: bool = False


@dataclass
class Volunteer:
    id: str
    label: str                     # masked display name, e.g. "Volunteer #4"
    vehicle_capacity: int          # crates
    cooler_capacity: int           # crates; 0 = no cooler
    availability: list[Window]


@dataclass
class Rejection:
    candidate_id: str
    candidate_name: str
    reason: str


# ---------- Settings ----------

SAFETY_BUFFER = timedelta(minutes=30)      # deliver at least this long before safe_until
RECEIVING_BUFFER = timedelta(minutes=15)   # arrive at least this long before a window closes


# ---------- Helpers ----------

def _fits_window(windows: list[Window], start: datetime, end: datetime,
                 end_buffer: timedelta = timedelta(0)) -> bool:
    """True if [start, end] sits inside one window on that weekday (same-day windows)."""
    if start.date() != end.date():
        return False
    for w in windows:
        if w.day != start.isoweekday():
            continue
        latest = datetime.combine(start.date(), w.end) - end_buffer
        if datetime.combine(start.date(), w.start) <= start and end <= latest:
            return True
    return False


def _describe_windows(windows: list[Window], day: int) -> str:
    today = [f"{w.start:%H:%M}-{w.end:%H:%M}" for w in windows if w.day == day]
    return ", ".join(today) if today else "no window that day"


# ---------- Receiver rules ----------
# Each rule returns None if OK, or a human-readable reason if it fails.

def check_receiving_window(r: Receiver, arrival: datetime) -> str | None:
    if not _fits_window(r.windows, arrival, arrival, end_buffer=RECEIVING_BUFFER):
        return (f"not receiving at arrival: {_describe_windows(r.windows, arrival.isoweekday())}, "
                f"ETA {arrival:%H:%M}")
    return None


def check_storage(r: Receiver, d: Donation) -> str | None:
    if d.storage_required not in r.storage_types:
        return f"no {d.storage_required.value} storage"
    return None


def check_capacity(r: Receiver, d: Donation) -> str | None:
    if r.available_capacity is not None and d.count > r.available_capacity:
        return f"not enough space: {d.count} crates, {r.available_capacity} available"
    return None


def check_food_type(r: Receiver, d: Donation) -> str | None:
    if r.accepted_food_types and d.food_type not in r.accepted_food_types:
        return f"doesn't accept {d.food_type}"
    return None


def check_food_safe_at(d: Donation, drop_off: datetime) -> str | None:
    if d.safe_until and drop_off > d.safe_until - SAFETY_BUFFER:
        return (f"food unsafe by drop-off: safe until {d.safe_until:%H:%M}, "
                f"drop-off {drop_off:%H:%M}")
    return None


def valid_receivers(
    donation: Donation,
    receivers: list[Receiver],
    arrival_times: dict[str, datetime],   # receiver_id -> estimated arrival
) -> tuple[list[Receiver], list[Rejection]]:
    valid, rejected = [], []
    for r in receivers:
        arrival = arrival_times.get(r.id)
        if arrival is None:
            rejected.append(Rejection(r.id, r.name, "no route estimate"))
            continue
        reasons = [x for x in (
            check_receiving_window(r, arrival),
            check_storage(r, donation),
            check_capacity(r, donation),
            check_food_type(r, donation),
            check_food_safe_at(donation, arrival),
        ) if x]
        if reasons:
            rejected.append(Rejection(r.id, r.name, "; ".join(reasons)))
        else:
            valid.append(r)
    return valid, rejected


# ---------- Volunteer rules ----------

def pickup_time(donation: Donation, arrival_at_donor: datetime) -> datetime:
    """A volunteer who arrives early waits until the food is ready."""
    return max(arrival_at_donor, donation.ready_at)


def valid_volunteers(
    donation: Donation,
    volunteers: list[Volunteer],
    arrival_at_donor: dict[str, datetime],   # volunteer_id -> when they'd reach the donor
    delivery_time: timedelta,                # donor -> receiver travel time
) -> tuple[list[Volunteer], list[Rejection]]:
    valid, rejected = [], []
    cold = donation.storage_required in (Storage.CHILLED, Storage.FROZEN)

    for v in volunteers:
        arrival = arrival_at_donor.get(v.id)
        if arrival is None:
            rejected.append(Rejection(v.id, v.label, "no route estimate"))
            continue
        pickup = pickup_time(donation, arrival)
        drop_off = pickup + delivery_time
        reasons = []

        if pickup > donation.pickup_deadline:
            reasons.append(f"misses pickup deadline: can pick up {pickup:%H:%M}, "
                           f"deadline {donation.pickup_deadline:%H:%M}")
        if not _fits_window(v.availability, arrival, drop_off):
            reasons.append(f"not available: {_describe_windows(v.availability, arrival.isoweekday())}, "
                           f"trip {arrival:%H:%M}-{drop_off:%H:%M}")
        if donation.count > v.vehicle_capacity:
            reasons.append(f"vehicle too small: {donation.count} crates, fits {v.vehicle_capacity}")
        if cold and donation.count > v.cooler_capacity:
            reasons.append("no cooler" if v.cooler_capacity == 0 else
                           f"cooler too small: {donation.count} crates, fits {v.cooler_capacity}")
        if (x := check_food_safe_at(donation, drop_off)):
            reasons.append(x)

        if reasons:
            rejected.append(Rejection(v.id, v.label, "; ".join(reasons)))
        else:
            valid.append(v)
    return valid, rejected


# ---------- Validator (guardrail for the agent's proposals) ----------

def validate_assignment(
    donation: Donation,
    receiver: Receiver,
    volunteer: Volunteer,
    arrival_at_donor: datetime,
    delivery_time: timedelta,
) -> list[str]:
    """Re-check one proposed assignment. Empty list = OK to save as pending."""
    drop_off = pickup_time(donation, arrival_at_donor) + delivery_time
    _, r_rej = valid_receivers(donation, [receiver], {receiver.id: drop_off})
    _, v_rej = valid_volunteers(donation, [volunteer], {volunteer.id: arrival_at_donor}, delivery_time)
    return [f"{x.candidate_name}: {x.reason}" for x in r_rej + v_rej]


# ---------- Quick demo: python -m matching.constraints ----------

if __name__ == "__main__":
    fri = datetime(2026, 11, 13, 21, 0)   # Friday 9:00pm, isoweekday 5

    def win(s, e): return [Window(5, time.fromisoformat(s), time.fromisoformat(e))]

    donation = Donation(
        id="don2", donor_id="d8", description="Trays from a cancelled catering order",
        food_type="cooked", count=6, storage_required=Storage.CHILLED,
        ready_at=fri, pickup_deadline=fri + timedelta(minutes=45),
        safe_until=fri + timedelta(hours=3),
    )
    receivers = [
        Receiver("r1", "Downtown Shelter", {Storage.AMBIENT, Storage.CHILLED, Storage.FROZEN},
                 win("07:00", "23:00"), 20),
        Receiver("r2", "Southside Food Bank", {Storage.AMBIENT, Storage.CHILLED, Storage.FROZEN},
                 win("09:00", "17:00"), 50),
        Receiver("r3", "Community Kitchen", {Storage.AMBIENT}, win("08:00", "22:00"), 12),
        Receiver("r4", "Youth Drop-in", {Storage.AMBIENT, Storage.CHILLED}, win("12:00", "23:30"), 3),
    ]
    arrivals = {r.id: fri + timedelta(minutes=40) for r in receivers}

    ok, rej = valid_receivers(donation, receivers, arrivals)
    print("Valid receivers:", [r.name for r in ok])
    for x in rej:
        print(f"  x {x.candidate_name}: {x.reason}")

    volunteers = [
        Volunteer("v1", "Volunteer #1", 10, 8, win("18:00", "23:00")),
        Volunteer("v2", "Volunteer #2", 10, 0, win("20:00", "22:30")),
        Volunteer("v3", "Volunteer #3", 10, 8, win("21:00", "21:30")),
        Volunteer("v4", "Volunteer #4", 10, 4, win("19:00", "23:30")),
    ]
    to_donor = {v.id: fri + timedelta(minutes=15) for v in volunteers}

    ok_v, rej_v = valid_volunteers(donation, volunteers, to_donor, timedelta(minutes=25))
    print("\nValid volunteers:", [v.label for v in ok_v])
    for x in rej_v:
        print(f"  x {x.candidate_name}: {x.reason}")

    print("\nAgent proposes Community Kitchen + Volunteer #2:")
    print(validate_assignment(donation, receivers[2], volunteers[1],
                              fri + timedelta(minutes=15), timedelta(minutes=25)))