"""
SeatSense AI — FastAPI Backend
================================
File structure:
    backend/
        main.py        ← this file
        test5.py       ← scraper + scorer logic (your file, untouched)
        routes.json    ← station km data

Run:
    pip install -r requirements.txt
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime

# ── import pure functions from test5 ──────────────────────────
from test5 import (
    load_train_route,
    fetch_all_classes,
    score_berth,
    COACH_CLASSES,
)

app = FastAPI(title="SeatSense AI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://seat-sense-alpha.vercel.app"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalyseRequest(BaseModel):
    train_no:  str
    date:      str   # DD/MM/YYYY (sent by frontend)
    from_code: str
    to_code:   str


# ── merged scorer ─────────────────────────────────────────────
def rank_all_classes(all_classes: dict, user_from: str, user_to: str,
                     station_km: dict) -> list:
    """
    Score every berth across ALL classes (SL + 2S).
    Returns a single list sorted by coverage_pct desc.
    Each item carries a 'class' key so the frontend can show the badge.
    """
    u_start_km = station_km[user_from]
    u_end_km   = station_km[user_to]
    results    = []

    for class_code, coaches in all_classes.items():
        for coach_name, coach_data in coaches.items():
            for berth in coach_data["bdd"]:
                score = score_berth(berth, u_start_km, u_end_km, station_km)
                if score == 0:
                    continue

                # Find best (longest) vacant segment inside the user's window
                best_seg  = None
                best_ovlp = 0.0
                vacant_km = 0.0

                for seg in berth["bsd"]:
                    if seg["occupancy"]:
                        continue
                    frm, to = seg["from"], seg["to"]
                    if frm not in station_km or to not in station_km:
                        continue
                    s_km   = station_km[frm]
                    e_km   = station_km[to]
                    ovlp_s = max(s_km, u_start_km)
                    ovlp_e = min(e_km, u_end_km)
                    if ovlp_e > ovlp_s:
                        ovlp = ovlp_e - ovlp_s
                        vacant_km += ovlp
                        if ovlp > best_ovlp:
                            best_ovlp = ovlp
                            best_seg  = (frm, to, s_km, e_km)

                if best_seg is None:
                    continue

                vac_from, vac_to, vac_from_km, vac_to_km = best_seg

                results.append({
                    "coach":        coach_name,
                    "berth_no":     berth["berthNo"],
                    "berth_type":   berth.get("berthCode", ""),
                    "class":        class_code,
                    "coverage_pct": score,
                    "overlap_km":   round(vacant_km, 1),
                    "vac_from":     vac_from,
                    "vac_to":       vac_to,
                    "vac_from_km":  vac_from_km,
                    "vac_to_km":    vac_to_km,
                })

    return sorted(results, key=lambda x: x["coverage_pct"], reverse=True)


# ── endpoints ─────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "SeatSense AI backend running"}


@app.post("/api/analyse")
def analyse(req: AnalyseRequest):
    try:
        journey_date = datetime.strptime(req.date.strip(), "%d/%m/%Y").strftime("%Y-%m-%d")

        train_no  = req.train_no.strip()
        from_code = req.from_code.strip().upper()
        to_code   = req.to_code.strip().upper()

        # Load route from routes.json
        route_stations, station_km, train_source = load_train_route(train_no)

        # Validate stations
        if from_code not in station_km:
            raise ValueError(
                f"Station '{from_code}' not found. "
                f"Available: {', '.join(route_stations)}"
            )
        if to_code not in station_km:
            raise ValueError(
                f"Station '{to_code}' not found. "
                f"Available: {', '.join(route_stations)}"
            )

        from_km = station_km[from_code]
        to_km   = station_km[to_code]

        if from_km >= to_km:
            raise ValueError(
                f"'{to_code}' ({to_km} km) does not come after "
                f"'{from_code}' ({from_km} km) on this route."
            )

        user_dist_km = to_km - from_km

        # Fetch all coaches (SL + 2S) — prints go to terminal, not response
        all_classes = fetch_all_classes(train_no, train_source, journey_date)

        if not all_classes:
            raise RuntimeError(
                "No coach data returned from IRCTC. "
                "Check train number, source station, or journey date."
            )

        # Score & rank across all classes
        ranked = rank_all_classes(all_classes, from_code, to_code, station_km)

        total_scanned = sum(
            len(cd["bdd"])
            for coaches in all_classes.values()
            for cd in coaches.values()
        )

        classes_found = list(all_classes.keys())
        class_label   = " · ".join(
            lbl for code, _, lbl in COACH_CLASSES if code in classes_found
        )

        return {
            "train_no":      train_no,
            "from_code":     from_code,
            "to_code":       to_code,
            "user_dist_km":  user_dist_km,
            "total_scanned": total_scanned,
            "eligible":      len(ranked),
            "class_label":   class_label,
            "ranked_seats":  ranked,
        }

    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")
