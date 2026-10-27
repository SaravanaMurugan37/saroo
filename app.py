"""Roamly AI Journey Planner: small Flask app with optional live data and AI."""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000

LIVE_DATA_ENABLED = os.getenv("LIVE_DATA_ENABLED", "false").lower() == "true"
NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
OSRM_URL = os.getenv("OSRM_URL", "https://router.project-osrm.org/route/v1/driving")
OPEN_METEO_URL = os.getenv("OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast")
USER_AGENT = os.getenv("GEOCODING_USER_AGENT", "RoamlyJourneyPlanner/1.0 (local demo)")
ALLOWED_LANGUAGES = {"en": "English", "ta": "Tamil", "hi": "Hindi"}
REQUEST_TIMEOUT = 5


def clean_text(value: Any, label: str, limit: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    value = value.strip()
    if required and not value:
        raise ValueError(f"Please enter {label.lower()}.")
    if len(value) > limit:
        raise ValueError(f"{label} must be {limit} characters or fewer.")
    return value


def parse_trip(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Please send trip details as a JSON object.")
    origin = clean_text(payload.get("origin"), "Starting location", 100)
    destination = clean_text(payload.get("destination"), "Destination", 100)
    try:
        start = date.fromisoformat(str(payload.get("start_date", "")))
        end = date.fromisoformat(str(payload.get("end_date", "")))
        travelers = int(payload.get("travelers"))
        budget = int(payload.get("budget"))
    except (TypeError, ValueError):
        raise ValueError("Enter valid dates, traveler count, and budget.") from None
    if end < start:
        raise ValueError("Return date must be on or after departure.")
    days = (end - start).days + 1
    if days > 14:
        raise ValueError("For this demo, trips can be up to 14 days.")
    if travelers < 1 or travelers > 20:
        raise ValueError("Traveler count must be between 1 and 20.")
    if budget < 1000 or budget > 100_000_000:
        raise ValueError("Budget must be between ₹1,000 and ₹10,00,00,000.")
    interests = payload.get("interests", [])
    if not isinstance(interests, list):
        interests = []
    allowed_interests = {"beaches", "mountains", "temples", "history", "adventure", "food", "nature", "shopping"}
    interests = [item for item in interests if isinstance(item, str) and item in allowed_interests][:8]
    language = payload.get("language", "en")
    if language not in ALLOWED_LANGUAGES:
        language = "en"
    return {
        "origin": origin, "destination": destination, "start_date": start,
        "end_date": end, "days": days, "travelers": travelers, "budget": budget,
        "interests": interests,
        "preferences": clean_text(payload.get("preferences", ""), "Preferences", 500, required=False),
        "language": language,
    }


def trip_dates(trip: dict[str, Any]) -> list[date]:
    return [trip["start_date"] + timedelta(days=index) for index in range(trip["days"])]


def estimate_budget(trip: dict[str, Any], route: dict[str, Any] | None = None) -> dict[str, Any]:
    """Use deterministic per-person planning allowances, never model-generated arithmetic."""
    people = trip["travelers"]
    days = trip["days"]
    nights = max(1, days - 1)
    # Transparent demo allowances in INR; these are not live vendor prices.
    route_km = (route or {}).get("distance_km")
    transit_allowance = 700 + (min(float(route_km), 1500) * 2 if route_km else 500)
    categories = {
        "transport": round(people * (transit_allowance + max(0, days - 1) * 250)),
        "accommodation": people * nights * 1400,
        "food": people * days * 700,
        "activities": people * days * 450,
    }
    total = sum(categories.values())
    day_costs = [0] * days
    for key, amount in categories.items():
        divisor = days if key != "accommodation" else nights
        quotient, remainder = divmod(amount, divisor)
        for index in range(divisor):
            target = index if key != "accommodation" else min(index, days - 1)
            day_costs[target] += quotient + (1 if index < remainder else 0)
    return {"categories": categories, "total": total, "remaining": trip["budget"] - total, "day_costs": day_costs}


def make_sample_itinerary(trip: dict[str, Any]) -> dict[str, Any]:
    """Location-neutral suggestions, clearly labeled as examples rather than facts."""
    interests = trip["interests"]
    preference = trip["preferences"]
    if "food" in interests:
        explore = "Ask a local for a well-reviewed regional breakfast spot"
    elif "history" in interests or "temples" in interests:
        explore = "Look up a locally significant heritage or cultural site"
    elif "nature" in interests or "mountains" in interests:
        explore = "Find a public garden, viewpoint, or easy nature walk"
    elif "beaches" in interests:
        explore = "Check local guidance for a public waterfront or beach visit"
    elif "adventure" in interests:
        explore = "Choose a reputable, locally operated outdoor activity"
    else:
        explore = "Take an easy orientation walk around your accommodation"
    days = []
    for index, trip_day in enumerate(trip_dates(trip)):
        days.append({
            "date": trip_day.isoformat(),
            "title": f"{trip['destination']} · Day {index + 1}",
            "area": "Flexible neighborhood plan",
            "estimated_cost": 0,
            "activities": [
                {"time": "morning", "name": explore if index == 0 else "Choose a nearby attraction that matches your interests", "description": "Sample idea only. Check opening hours, access, and local advice before setting out.", "cost": 0},
                {"time": "afternoon", "name": "A relaxed local lunch", "description": "Look for a well-reviewed restaurant; tell the staff about any dietary requirements.", "cost": 0},
                {"time": "evening", "name": "Unhurried time to explore", "description": "Pick a public, well-lit area or a local cultural activity that is open on the day.", "cost": 0},
            ],
        })
    if preference and days:
        days[0]["activities"][0]["description"] = f"Preference to consider: {preference}. Sample idea only; verify details locally."
    return {"days": days}


def add_deterministic_costs(itinerary: dict[str, Any], trip: dict[str, Any], route: dict[str, Any] | None = None) -> dict[str, Any]:
    budget = estimate_budget(trip, route)
    for index, day in enumerate(itinerary["days"]):
        activities = day.get("activities", [])
        if not isinstance(activities, list):
            activities = []
            day["activities"] = activities
        # Put the food allowance on the lunch suggestion; distribute activity allowance
        # across the remaining recommendations. Transportation and lodging remain in day total.
        food_share = budget["categories"]["food"] // trip["days"]
        activity_share = budget["categories"]["activities"] // trip["days"]
        for item_index, activity in enumerate(activities):
            if not isinstance(activity, dict):
                continue
            activity["time"] = activity.get("time") if activity.get("time") in {"morning", "afternoon", "evening"} else ("morning", "afternoon", "evening")[min(item_index, 2)]
            if item_index == 1:
                activity["cost"] = food_share
            elif item_index > 1:
                activity["cost"] = 0
            else:
                activity["cost"] = activity_share
        day["estimated_cost"] = budget["day_costs"][index]
    itinerary["budget"] = {"categories": budget["categories"], "total": budget["total"], "remaining": budget["remaining"]}
    return itinerary


def ai_client() -> Any | None:
    if not os.getenv("OPENAI_API_KEY"):
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except ImportError:
        app.logger.warning("OpenAI package is not installed; using the labeled sample itinerary.")
        return None


def create_ai_itinerary(trip: dict[str, Any]) -> dict[str, Any] | None:
    client = ai_client()
    if client is None:
        return None
    prompt = {
        "origin": trip["origin"], "destination": trip["destination"],
        "dates": [day.isoformat() for day in trip_dates(trip)], "travelers": trip["travelers"],
        "interests": trip["interests"], "preferences": trip["preferences"],
        "language": ALLOWED_LANGUAGES[trip["language"]],
    }
    try:
        result = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Create a practical travel itinerary. Return JSON only with {days:[{date,title,area,activities:[{time,name,description}]}]}. Every day needs 2-4 activities. Write all user-facing text in the requested language. Do not claim real-time opening hours, prices, weather, availability, or route data. Treat attractions as suggestions to verify. Do not include any costs."},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            max_tokens=2600,
            timeout=30,
        )
        raw = result.choices[0].message.content or "{}"
        data = json.loads(raw)
        days = data.get("days") if isinstance(data, dict) else None
        if not isinstance(days, list) or not days:
            return None
        normalized = []
        for index, trip_day in enumerate(trip_dates(trip)):
            candidate = days[index] if index < len(days) and isinstance(days[index], dict) else {}
            activities = candidate.get("activities", [])
            if not isinstance(activities, list):
                activities = []
            clean_activities = []
            for activity in activities[:4]:
                if not isinstance(activity, dict):
                    continue
                clean_activities.append({
                    "time": activity.get("time", "morning") if activity.get("time") in {"morning", "afternoon", "evening"} else "morning",
                    "name": clean_text(activity.get("name", "Travel at your own pace"), "Activity name", 120),
                    "description": clean_text(activity.get("description", "Suggested activity; verify local details."), "Activity description", 300, required=False),
                    "cost": 0,
                })
            if len(clean_activities) < 2:
                clean_activities.extend(make_sample_itinerary({**trip, "days": 1, "start_date": trip_day, "end_date": trip_day})["days"][0]["activities"][:2 - len(clean_activities)])
            normalized.append({
                "date": trip_day.isoformat(),
                "title": clean_text(candidate.get("title", f"{trip['destination']} · Day {index + 1}"), "Day title", 120),
                "area": clean_text(candidate.get("area", "Explore at your own pace"), "Area", 100, required=False),
                "estimated_cost": 0, "activities": clean_activities,
            })
        return {"days": normalized}
    except Exception as error:  # Network, provider, or response parsing failures use transparent sample mode.
        app.logger.warning("AI itinerary unavailable; using sample plan (%s).", type(error).__name__)
        return None


@lru_cache(maxsize=64)
def geocode(place: str) -> tuple[float, float] | None:
    if not LIVE_DATA_ENABLED:
        return None
    try:
        response = requests.get(NOMINATIM_URL, params={"q": place, "format": "jsonv2", "limit": 1}, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        results = response.json()
        if results:
            return float(results[0]["lon"]), float(results[0]["lat"])
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
        app.logger.info("Geocoding unavailable (%s).", type(error).__name__)
    return None


def fetch_route(origin: str, destination: str) -> dict[str, Any]:
    unavailable = {"source": "sample", "mode": "—", "note": "Illustrative route only · live distance and travel time unavailable."}
    if not LIVE_DATA_ENABLED:
        return unavailable
    start, end = geocode(origin), geocode(destination)
    if not start or not end:
        return unavailable
    try:
        url = f"{OSRM_URL.rstrip('/')}/{start[0]},{start[1]};{end[0]},{end[1]}"
        response = requests.get(url, params={"overview": "false"}, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        routes = response.json().get("routes", [])
        if routes:
            route = routes[0]
            return {"source": "live", "mode": "Road", "distance_km": round(route["distance"] / 1000), "duration_hours": round(route["duration"] / 3600, 1), "note": "Live road-route estimate · driving time can vary with traffic."}
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
        app.logger.info("Routing unavailable (%s).", type(error).__name__)
    return unavailable


def weather_code_label(code: int) -> str:
    if code == 0:
        return "Clear"
    if code in {1, 2}:
        return "Mostly clear"
    if code == 3:
        return "Cloudy"
    if code in {45, 48}:
        return "Fog"
    if code in {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}:
        return "Rain possible"
    if code in {71, 73, 75, 77, 85, 86}:
        return "Snow possible"
    if code in {95, 96, 99}:
        return "Thunderstorms"
    return "Forecast available"


def fetch_weather(destination: str, trip: dict[str, Any]) -> dict[str, Any] | None:
    if not LIVE_DATA_ENABLED:
        return None
    coords = geocode(destination)
    if not coords or trip["end_date"] > date.today() + timedelta(days=14):
        return None
    try:
        response = requests.get(OPEN_METEO_URL, params={
            "latitude": coords[1], "longitude": coords[0],
            "daily": "weather_code,temperature_2m_max,precipitation_probability_max",
            "timezone": "auto", "start_date": trip["start_date"].isoformat(), "end_date": trip["end_date"].isoformat(),
        }, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        daily = response.json().get("daily", {})
        codes = daily.get("weather_code", [])
        if not codes:
            return None
        label = weather_code_label(int(codes[0]))
        high = daily.get("temperature_2m_max", [None])[0]
        rain = daily.get("precipitation_probability_max", [None])[0]
        summary = f"{label} · forecast for {len(codes)} day(s)"
        if high is not None:
            summary += f" · first-day high {round(float(high))}°C"
        advice = "Pack a light rain layer and keep an indoor option handy." if rain is not None and float(rain) >= 40 else "Forecasts can change; check again close to departure."
        return {"source": "live", "summary": summary, "short": label, "advice": advice}
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as error:
        app.logger.info("Weather unavailable (%s).", type(error).__name__)
    return None


def localized_reply(language: str, kind: str) -> str:
    replies = {
        "en": {"updated": "Done — I’ve adjusted your plan. Review the refreshed day cards and estimates.", "clarify": "I can make the plan slower, add local food or nature suggestions, or add a flexible break. Try one of those requests."},
        "ta": {"updated": "சரி — உங்கள் திட்டத்தை மாற்றியுள்ளேன். புதுப்பிக்கப்பட்ட நாட்களையும் மதிப்பீடுகளையும் பாருங்கள்.", "clarify": "பயணத்தை மெதுவாக்க, உள்ளூர் உணவு அல்லது இயற்கை பரிந்துரைகளைச் சேர்க்க, அல்லது ஓய்வு நேரம் சேர்க்கலாம்."},
        "hi": {"updated": "हो गया — आपकी योजना बदल दी है। अपडेट किए गए दिन और अनुमान देखें।", "clarify": "मैं यात्रा की रफ़्तार धीमी कर सकता हूँ, स्थानीय भोजन या प्रकृति के सुझाव जोड़ सकता हूँ, या आराम का समय रख सकता हूँ।"},
    }
    return replies.get(language, replies["en"])[kind]


def modify_itinerary_with_ai(message: str, itinerary: dict[str, Any], trip: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    client = ai_client()
    if client is None:
        return None
    try:
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), response_format={"type": "json_object"}, max_tokens=2200, timeout=30,
            messages=[
                {"role": "system", "content": f"Modify the provided travel plan according to the user's request. Return JSON only with {{reply:string, days:[{{date,title,area,activities:[{{time,name,description}}]}}]}}. Keep exactly {trip['days']} days, 2-4 activities daily. Write in {ALLOWED_LANGUAGES[trip['language']]}. Treat destinations as suggestions to verify. Do not include costs or claim live details."},
                {"role": "user", "content": json.dumps({"request": message, "plan": itinerary["days"]}, ensure_ascii=False)},
            ],
        )
        data = json.loads(response.choices[0].message.content or "{}")
        if not isinstance(data.get("days"), list) or not data["days"]:
            return None
        modified = {"days": []}
        for index, original in enumerate(itinerary["days"]):
            entry = data["days"][index] if index < len(data["days"]) and isinstance(data["days"][index], dict) else original
            activities = []
            for activity in entry.get("activities", [])[:4]:
                if isinstance(activity, dict):
                    activities.append({"time": activity.get("time", "morning"), "name": clean_text(activity.get("name", "Explore at your own pace"), "Activity name", 120), "description": clean_text(activity.get("description", "Suggested activity; verify local details."), "Activity description", 300, required=False), "cost": 0})
            if len(activities) < 2:
                activities = original["activities"]
            modified["days"].append({"date": original["date"], "title": clean_text(entry.get("title", original["title"]), "Day title", 120), "area": clean_text(entry.get("area", original.get("area", "")), "Area", 100, required=False), "activities": activities, "estimated_cost": 0})
        return modified, clean_text(data.get("reply", localized_reply(trip["language"], "updated")), "Reply", 500, required=False)
    except Exception as error:
        app.logger.info("AI itinerary edit unavailable (%s).", type(error).__name__)
        return None


def deterministic_chat(message: str, itinerary: dict[str, Any], trip: dict[str, Any]) -> tuple[dict[str, Any], str]:
    text = message.lower()
    changed = False
    for day in itinerary["days"]:
        activities = day["activities"]
        if any(term in text for term in ("relax", "slow", "leisure", "break", "आराम", "धीम", "மெதுவ", "ஓய்வு")):
            if activities:
                activities[-1] = {"time": "evening", "name": "Flexible downtime and a relaxed evening", "description": "A deliberately open block to rest or choose an easy nearby option.", "cost": 0}
            changed = True
        if any(term in text for term in ("food", "restaurant", "cafe", "café", "eat", "உணவு", "சாப்ப", "खाना", "भोजन")):
            if len(activities) >= 2:
                activities[1] = {"time": "afternoon", "name": "Explore a local food market or well-reviewed regional café", "description": "Check current opening hours and mention any dietary needs.", "cost": 0}
            changed = True
        if any(term in text for term in ("nature", "garden", "park", "outdoor", "இயற்கை", "प्रकृति", "बगीचा")):
            if activities:
                activities[0] = {"time": "morning", "name": "Add a nearby nature walk or public garden", "description": "Choose an accessible place and check local conditions before setting out.", "cost": 0}
            changed = True
    if not changed:
        return itinerary, localized_reply(trip["language"], "clarify")
    return itinerary, localized_reply(trip["language"], "updated")


@app.get("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/<path:filename>")
def project_file(filename: str):
    # Only serve the two frontend assets; language files are exposed through a validated route.
    if filename in {"style.css", "script.js"}:
        return send_from_directory(BASE_DIR, filename)
    return jsonify({"error": "Not found."}), 404


@app.get("/api/languages/<language>")
def language_file(language: str):
    if language not in ALLOWED_LANGUAGES:
        return jsonify({"error": "Language not supported."}), 404
    return send_from_directory(BASE_DIR / "lang", f"{language}.json")


@app.post("/api/plan")
def plan_trip():
    try:
        trip = parse_trip(request.get_json(silent=True))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    route = fetch_route(trip["origin"], trip["destination"])
    weather = fetch_weather(trip["destination"], trip)
    itinerary = create_ai_itinerary(trip)
    ai_generated = itinerary is not None
    if itinerary is None:
        itinerary = make_sample_itinerary(trip)
    itinerary = add_deterministic_costs(itinerary, trip, route if route.get("source") == "live" else None)
    return jsonify({
        "itinerary": itinerary, "budget": itinerary["budget"], "route": route,
        "weather": weather, "ai_generated": ai_generated,
        "data_mode": "live" if LIVE_DATA_ENABLED else "sample",
    })


@app.post("/api/chat")
def chat():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Send a message and trip plan."}), 400
    try:
        message = clean_text(payload.get("message"), "Message", 500)
        trip = parse_trip(payload.get("trip"))
        incoming = payload.get("itinerary")
        if not isinstance(incoming, dict) or not isinstance(incoming.get("days"), list) or len(incoming["days"]) != trip["days"]:
            raise ValueError("The itinerary is missing or does not match your trip dates. Generate the journey again.")
        safe_days = []
        for index, day in enumerate(incoming["days"]):
            if not isinstance(day, dict):
                raise ValueError("The itinerary could not be read. Generate the journey again.")
            activities = day.get("activities", [])
            if not isinstance(activities, list):
                activities = []
            safe_activities = []
            for activity in activities[:4]:
                if isinstance(activity, dict):
                    safe_activities.append({"time": activity.get("time", "morning"), "name": clean_text(activity.get("name", "Explore at your own pace"), "Activity name", 120), "description": clean_text(activity.get("description", ""), "Activity description", 300, required=False), "cost": 0})
            safe_days.append({"date": trip_dates(trip)[index].isoformat(), "title": clean_text(day.get("title", f"Day {index + 1}"), "Day title", 120), "area": clean_text(day.get("area", ""), "Area", 100, required=False), "activities": safe_activities, "estimated_cost": 0})
        itinerary = {"days": safe_days}
        updated = modify_itinerary_with_ai(message, itinerary, trip)
        if updated is None:
            itinerary, reply = deterministic_chat(message, itinerary, trip)
        else:
            itinerary, reply = updated
        itinerary = add_deterministic_costs(itinerary, trip)
        return jsonify({"itinerary": itinerary, "budget": itinerary["budget"], "reply": reply})
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "ai_configured": bool(os.getenv("OPENAI_API_KEY")), "live_data_enabled": LIVE_DATA_ENABLED})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
