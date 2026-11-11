"""
Data access layer. Everything else (agent tools, API, matching) calls these functions
and never reads seed.json directly. seed.json has one list per DB table; when the
Supabase tables exist, rewrite the insides of the read/write functions to query them.
Nothing else needs to change.
"""

import json
import uuid
from collections import defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path

from matching.constraints import Donation, Receiver, Storage, Volunteer, Window

_SEED = json.loads((Path(__file__).parent / "seed.json").read_text())
_SAFE_HOURS = _SEED["food_type_safe_hours"]

# Tables the schema doesn't have yet (see notes): kept in memory, lost on restart.
_assignments: list[dict] = []
_decision_log: list[dict] = []
_new_donations: list[dict] = []


def _windows(rows: list[dict], key: str) -> dict[str, list[Window]]:
    out = defaultdict(list)
    for w in rows:
        out[w[key]].append(Window(w["day"], time.fromisoformat(w["start_time"]),
                                  time.fromisoformat(w["end_time"])))
    return out


def _to_donation(row: dict) -> Donation:
    ready = datetime.fromisoformat(row["ready_at"])
    hours = _SAFE_HOURS.get(row["food_type"])
    return Donation(
        id=row["id"], donor_id=row["donor_id"], description=row["description"],
        food_type=row["food_type"], count=row["count"],
        storage_required=Storage(row["storage_required"]),
        ready_at=ready, pickup_deadline=datetime.fromisoformat(row["pickup_deadline"]),
        safe_until=ready + timedelta(hours=hours) if hours else None,
    )


# ---------- Reads ----------

def get_receivers() -> list[Receiver]:
    windows = _windows(_SEED["receiving_window"], "organisation_id")
    return [Receiver(
        id=o["id"], name=o["name"],
        storage_types={Storage(s) for s in o["storage_types"] or []},
        windows=windows[o["id"]],
        available_capacity=o["available_capacity"],
        accepted_food_types=set(o["accepted_food_types"] or []),
        requires_confirmation=bool(o["requires_confirmation"]),
    ) for o in _SEED["organization"] if o["role"] == "receiver"]


def get_donors() -> list[dict]:
    return [o for o in _SEED["organization"] if o["role"] == "donor"]


def get_volunteers() -> list[Volunteer]:
    """Names are masked: the agent and dashboards only ever see 'Volunteer #n'."""
    windows = _windows(_SEED["volunteer_availability"], "volunteer_id")
    return [Volunteer(
        id=v["id"], label=f"Volunteer #{i}",
        vehicle_capacity=v["vehicle_capacity"], cooler_capacity=v["cooler_capacity"],
        availability=windows[v["id"]],
    ) for i, v in enumerate(_SEED["volunteer"], start=1)]


def get_donations() -> list[Donation]:
    return [_to_donation(d) for d in _SEED["donation"] + _new_donations]


def get_locations() -> dict[str, tuple[float, float]]:
    """id -> (lat, lng) for organizations and volunteers. Used by routing and the map."""
    rows = _SEED["organization"] + _SEED["volunteer"]
    return {r["id"]: (r["latitude"], r["longitude"]) for r in rows if r.get("latitude") is not None}


# ---------- Writes ----------

def add_donation(donor_id: str, description: str, food_type: str, count: int,
                 storage_required: str, ready_at: str, pickup_deadline: str) -> Donation:
    row = {"id": str(uuid.uuid4()), "donor_id": donor_id, "description": description,
           "food_type": food_type, "count": count, "storage_required": storage_required,
           "ready_at": ready_at, "pickup_deadline": pickup_deadline}
    _new_donations.append(row)
    return _to_donation(row)


def save_assignment(donation_id: str, receiver_id: str, volunteer_id: str,
                    pickup_eta: datetime, delivery_eta: datetime, reason: str,
                    status: str = "pending") -> dict:
    row = {"id": str(uuid.uuid4()), "donation_id": donation_id, "receiver_id": receiver_id,
           "volunteer_id": volunteer_id, "pickup_eta": pickup_eta.isoformat(),
           "delivery_eta": delivery_eta.isoformat(), "reason": reason, "status": status}
    _assignments.append(row)
    return row


def get_assignments() -> list[dict]:
    return list(_assignments)


def log_decision(action: str, reason: str, donation_id: str | None = None) -> None:
    _decision_log.append({"id": str(uuid.uuid4()),
                          "time": datetime.now().isoformat(timespec="seconds"),
                          "donation_id": donation_id, "action": action, "reason": reason})


def get_decision_log() -> list[dict]:
    return list(_decision_log)