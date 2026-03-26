"""
SeatSense AI — Live Seat Scorer (km-based scoring)
====================================================
Usage:
    python test4.py

Requires:
    pip install requests
    surat_to_ahmedabad_trains_with_distance.json  (in same directory)
"""

import json
import time
import requests
from datetime import date

# ─────────────────────────────────────────────
BOARDING_STATION = "ST"
MAX_COACHES      = 20
DELAY_BETWEEN    = 0.4
ROUTE_JSON       = "surat_to_ahmedabad_trains_with_distance.json"

# Each entry: (class_code, coach_prefix, display_label)
COACH_CLASSES = [
    ("SL", "S", "Sleeper (SL)"),
    ("2S", "D", "Second Sitting (2S)"),
]
# ─────────────────────────────────────────────

API_URL = "https://www.irctc.co.in/online-charts/api/coachComposition"
HEADERS = {
    "accept":       "application/json",
    "content-type": "application/json",
    "origin":       "https://www.irctc.co.in",
    "referer":      "https://www.irctc.co.in/online-charts/traincomposition",
    "user-agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/146.0.0.0 Safari/537.36",
    "dnt":          "1",
}


# ── Route loading from JSON ────────────────────────────────────
def load_train_route(train_no: str):
    """
    Load station list for a given train from the local JSON file.

    Returns:
        route_stations : list of station codes in travel order  e.g. ["ST","KIM",...]
        station_km     : dict  station_code → distance_from_origin_km
        train_source   : origin code stored in JSON  e.g. "BDTS"

    Raises FileNotFoundError / ValueError if train not found.
    """
    with open(ROUTE_JSON, encoding="utf-8") as f:
        trains = json.load(f)

    for t in trains:
        if t["train_no"] == train_no:
            # Stations are already ordered by ascending km in the JSON
            route_stations = [s["code"] for s in t["stations"]]
            station_km     = {s["code"]: s["distance_from_origin_km"]
                              for s in t["stations"]}
            return route_stations, station_km, t["origin"]

    available = ", ".join(f"{t['train_no']} ({t['train_name']})" for t in trains)
    raise ValueError(
        f"Train {train_no!r} not found in {ROUTE_JSON}.\n"
        f"  Available: {available}"
    )


# ── Fetch ──────────────────────────────────────────────────────
def fetch_class_coaches(session, train_no: str, train_source: str,
                        journey_date: str, class_code: str,
                        coach_prefix: str) -> dict:
    """
    Fetch all coaches for one class (e.g. SL -> S1..S20, 2S -> D1..D20).
    Stops after 3 consecutive misses.
    Returns dict  coach_name -> API response data.
    """
    coaches = {}
    misses  = 0

    for i in range(1, MAX_COACHES + 1):
        coach   = f"{coach_prefix}{i}"
        payload = {
            "trainNo":            train_no,
            "boardingStation":    BOARDING_STATION,
            "remoteStation":      BOARDING_STATION,
            "trainSourceStation": train_source,
            "jDate":              journey_date,
            "coach":              coach,
            "cls":                class_code,
        }
        try:
            resp = session.post(API_URL, json=payload, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if data.get("error") or not data.get("bdd"):
                misses += 1
                print(f"  {coach}✗", end="", flush=True)
            else:
                misses = 0
                coaches[coach] = data
                print(f"  {coach}✓", end="", flush=True)

        except Exception:
            misses += 1
            print(f"  {coach}✗", end="", flush=True)

        if misses >= 3:
            break

        time.sleep(DELAY_BETWEEN)

    return coaches


def fetch_all_classes(train_no: str, train_source: str,
                      journey_date: str) -> dict:
    """
    Fetch coaches for ALL configured classes (SL + 2S).
    Returns dict  class_code -> { coach_name -> API data }
    e.g. { "SL": {"S1": {...}, "S2": {...}}, "2S": {"D1": {...}} }
    """
    session = requests.Session()
    result  = {}

    for class_code, coach_prefix, label in COACH_CLASSES:
        print(f"\n  [{label}] fetching coaches", end="", flush=True)
        coaches = fetch_class_coaches(
            session, train_no, train_source,
            journey_date, class_code, coach_prefix
        )
        print(f"  -> {len(coaches)} coach(es) found")
        if coaches:
            result[class_code] = coaches

    return result


# ── Scoring (km-based) ─────────────────────────────────────────
def score_berth(berth: dict, u_start_km: int, u_end_km: int,
                station_km: dict) -> float:
    """
    Coverage score = (vacant km overlapping user journey) / (user journey km) × 100

    A segment is considered only when BOTH its endpoints exist in station_km
    (i.e. they appear in the JSON route for this train).
    """
    journey_km = u_end_km - u_start_km
    vacant_km  = 0.0

    for seg in berth["bsd"]:
        if seg["occupancy"]:          # seat is occupied on this segment
            continue
        frm, to = seg["from"], seg["to"]

        # Skip segments whose stations aren't in our JSON route
        if frm not in station_km or to not in station_km:
            continue

        seg_start_km = station_km[frm]
        seg_end_km   = station_km[to]

        # Clamp to user's journey window
        overlap_start = max(seg_start_km, u_start_km)
        overlap_end   = min(seg_end_km,   u_end_km)

        if overlap_end > overlap_start:
            vacant_km += (overlap_end - overlap_start)

    return round((vacant_km / journey_km) * 100, 1)


def rank_all(coaches: dict, user_from: str, user_to: str,
             station_km: dict) -> list:
    u_start_km = station_km[user_from]
    u_end_km   = station_km[user_to]
    results    = []

    for coach_name, coach_data in coaches.items():
        for berth in coach_data["bdd"]:
            score = score_berth(berth, u_start_km, u_end_km, station_km)
            if score == 0:
                continue

            segs = " | ".join(
                f"{s['from']}→{s['to']} "
                f"[{'free' if not s['occupancy'] else 'occ'}]"
                for s in berth["bsd"]
            )
            results.append({
                "coach":   coach_name,
                "berthNo": berth["berthNo"],
                "type":    berth["berthCode"],
                "cabin":   berth["cabinCoupeNameNo"],
                "score":   score,
                "segs":    segs,
            })

    return sorted(results, key=lambda x: x["score"], reverse=True)


# ── Display ────────────────────────────────────────────────────
def display(ranked: list, user_from: str, user_to: str,
            journey_km: int, label: str) -> None:
    print()
    print("=" * 70)
    print(f"  {label}  —  {user_from} -> {user_to}  ({journey_km} km)")
    print("=" * 70)
    print(f"  {'#':<4} {'Coach':<6} {'Berth':<7} {'Type':<5} {'Cabin':<6} {'Score'}")
    print("-" * 70)

    for i, r in enumerate(ranked[:20], 1):
        bar = "█" * int(r["score"] / 10)
        print(f"  {i:<4} {r['coach']:<6} #{r['berthNo']:<6} {r['type']:<5} "
              f"C{r['cabin']:<5} {r['score']:>5.1f}%  {bar}")
        print(f"       {r['segs']}")

    print("=" * 70)
    print(f"  Showing top {min(20, len(ranked))} of {len(ranked)} berths with vacancy")
    print(f"  100% score : {sum(1 for r in ranked if r['score'] == 100.0)} berths")
    print("=" * 70)


# ── Main ───────────────────────────────────────────────────────
def main():
    print("=" * 70)
    print("  SeatSense AI — Live Seat Scorer")
    print("=" * 70)

    train_no  = input("\n  Train number         : ").strip()
    user_from = input("  Your boarding stn    : ").strip().upper()
    user_to   = input("  Your destination stn : ").strip().upper()

    # ── Load route from JSON ──────────────────────────────────
    try:
        route_stations, station_km, train_source = load_train_route(train_no)
    except (FileNotFoundError, ValueError) as e:
        print(f"\n  ❌ Route error: {e}")
        return

    print(f"\n  Train source (from JSON) : {train_source}")
    print(f"  Route ({len(route_stations)} stations):")
    print(f"  {' → '.join(route_stations)}")

    # ── Validate user stations ────────────────────────────────
    if user_from not in station_km:
        print(f"\n  ❌ '{user_from}' not found in JSON route.")
        print(f"  Available: {', '.join(route_stations)}")
        return
    if user_to not in station_km:
        print(f"\n  ❌ '{user_to}' not found in JSON route.")
        print(f"  Available: {', '.join(route_stations)}")
        return

    from_km = station_km[user_from]
    to_km   = station_km[user_to]

    if from_km >= to_km:
        print(f"\n  ❌ '{user_to}' ({to_km} km) does not come after "
              f"'{user_from}' ({from_km} km) on this route.")
        return

    journey_km = to_km - from_km
    print(f"\n  Journey : {user_from} ({from_km} km) → {user_to} ({to_km} km)"
          f"  =  {journey_km} km")

    # ── Fetch live coach data ─────────────────────────────────
    journey_date = date.today().strftime("%Y-%m-%d")
    print(f"  Date    : {journey_date}  (today)")

    all_classes = fetch_all_classes(train_no, train_source, journey_date)

    if not all_classes:
        print("\n  No data returned for any class. Check train number / source / date.")
        return

    total_coaches = sum(len(c) for c in all_classes.values())
    print(f"\n  Total coaches fetched : {total_coaches} "
          f"across {len(all_classes)} class(es)")

    # ── Score & display per class ─────────────────────────────
    for class_code, coach_prefix, label in COACH_CLASSES:
        coaches = all_classes.get(class_code)
        if not coaches:
            print(f"\n  [{label}]  No coaches found — skipping.")
            continue
        ranked = rank_all(coaches, user_from, user_to, station_km)
        display(ranked, user_from, user_to, journey_km, label)


if __name__ == "__main__":
    main()
