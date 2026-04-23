"""Alfred's data source connectors, weather, calendar, and more."""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from database import get_db
from config import DATA_DIR

# Cache to avoid hitting APIs on every single message
_cache = {}
_CACHE_TTL = 300  # 5 minutes

def _get_cached(key, fetch_fn):
    import time
    now = time.time()
    if key in _cache and now - _cache[key]['ts'] < _CACHE_TTL:
        return _cache[key]['data']
    try:
        data = fetch_fn()
        _cache[key] = {'data': data, 'ts': now}
        return data
    except Exception:
        return _cache.get(key, {}).get('data', '')


# ============================================================
# WEATHER (no auth needed)
# ============================================================

def get_weather(lat=42.48, lon=-71.20):
    """Get current weather for Burlington, MA area using Open-Meteo (free, no API key)."""
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code"
        f"&temperature_unit=fahrenheit"
        f"&wind_speed_unit=mph"
        f"&timezone=America/New_York"
        f"&forecast_days=3"
    )
    
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        
        current = data.get("current", {})
        daily = data.get("daily", {})
        
        weather_codes = {
            0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
            45: "Foggy", 48: "Depositing rime fog",
            51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
            61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
            71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
            80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
            95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail"
        }
        
        code = current.get("weather_code", 0)
        condition = weather_codes.get(code, f"Code {code}")
        
        result = {
            "current": {
                "temperature": current.get("temperature_2m"),
                "feels_like": current.get("apparent_temperature"),
                "condition": condition,
                "humidity": current.get("relative_humidity_2m"),
                "wind_speed": current.get("wind_speed_10m")
            },
            "forecast": []
        }
        
        if daily.get("time"):
            for i in range(len(daily["time"])):
                day_code = daily.get("weather_code", [0])[i] if i < len(daily.get("weather_code", [])) else 0
                result["forecast"].append({
                    "date": daily["time"][i],
                    "high": daily.get("temperature_2m_max", [None])[i],
                    "low": daily.get("temperature_2m_min", [None])[i],
                    "precipitation": daily.get("precipitation_sum", [0])[i],
                    "condition": weather_codes.get(day_code, f"Code {day_code}")
                })
        
        return result
    except Exception as e:
        return {"error": str(e)}


def get_weather_summary():
    """Get a formatted weather summary for Alfred's context."""
    weather = get_weather()
    if "error" in weather:
        return ""
    
    c = weather["current"]
    lines = [f"## Current Weather"]
    lines.append(f"- {c['condition']}, {c['temperature']}°F (feels like {c['feels_like']}°F)")
    lines.append(f"- Humidity: {c['humidity']}%, Wind: {c['wind_speed']} mph")
    
    if weather.get("forecast"):
        lines.append("- Forecast:")
        for f in weather["forecast"][:2]:
            lines.append(f"  - {f['date']}: {f['condition']}, {f['low']}°F - {f['high']}°F")
    
    return "\n".join(lines)


# ============================================================
# TIME CONTEXT
# ============================================================

def get_time_context():
    """Get current time context for Alfred."""
    from dateutil import tz
    now = datetime.now(tz.gettz("America/New_York"))
    
    hour = now.hour
    if hour < 6:
        period = "very early morning"
    elif hour < 9:
        period = "morning"
    elif hour < 12:
        period = "late morning"
    elif hour < 14:
        period = "early afternoon"
    elif hour < 17:
        period = "afternoon"
    elif hour < 20:
        period = "evening"
    elif hour < 23:
        period = "late evening"
    else:
        period = "night"
    
    return {
        "datetime": now.strftime("%A, %B %d, %Y at %I:%M %p %Z"),
        "period": period,
        "day_of_week": now.strftime("%A"),
        "is_weekend": now.weekday() >= 5
    }


def get_time_summary():
    """Get formatted time context."""
    t = get_time_context()
    weekend = " (weekend)" if t["is_weekend"] else ""
    return f"## Current Time\n- {t['datetime']}{weekend}, {t['period']}"


# ============================================================
# COMBINED CONTEXT
# ============================================================

def get_data_context():
    """Get all data source context combined for Alfred's system prompt."""
    parts = []
    
    time_ctx = get_time_summary()
    if time_ctx:
        parts.append(time_ctx)
    
    weather_ctx = get_weather_summary()
    if weather_ctx:
        parts.append(weather_ctx)
    
    return "\n\n".join(parts)


if __name__ == "__main__":
    print("=== Time ===")
    print(get_time_summary())
    print("\n=== Weather ===")
    print(get_weather_summary())


# ============================================================
# TODOIST
# ============================================================

TODOIST_API_KEY = os.environ.get("TODOIST_API_KEY", "")

def get_todoist_tasks():
    """Get active tasks from Todoist (API v1)."""
    key = os.environ.get("TODOIST_API_KEY", "") or TODOIST_API_KEY
    if not key:
        return {"error": "Todoist API key not set"}
    
    req = urllib.request.Request(
        "https://api.todoist.com/api/v1/tasks",
        headers={"Authorization": f"Bearer {key}"}
    )
    
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        
        tasks = data.get("results", []) if isinstance(data, dict) else data
        
        result = []
        for t in tasks:
            due = t.get("due") or t.get("deadline") or {}
            due_date = due.get("date") if isinstance(due, dict) else None
            due_str = due.get("string", due_date) if isinstance(due, dict) else None
            
            result.append({
                "id": t.get("id"),
                "content": t.get("content", ""),
                "description": t.get("description", ""),
                "priority": t.get("priority", 1),
                "due": due_str,
                "due_date": due_date,
                "labels": t.get("labels", [])
            })
        
        return sorted(result, key=lambda x: (x["due_date"] or "9999", -x["priority"]))
    except Exception as e:
        return {"error": str(e)}


def get_todoist_summary():
    """Get formatted Todoist task summary for Alfred's context."""
    tasks = get_todoist_tasks()
    if isinstance(tasks, dict) and "error" in tasks:
        return ""
    if not tasks:
        return "## Tasks\n- No active tasks"
    
    lines = ["## Tasks (Todoist)"]
    
    # Group by due date
    today = datetime.now().strftime("%Y-%m-%d")
    overdue = [t for t in tasks if t.get("due_date") and t["due_date"] < today]
    due_today = [t for t in tasks if t.get("due_date") == today]
    upcoming = [t for t in tasks if t.get("due_date") and t["due_date"] > today]
    no_date = [t for t in tasks if not t.get("due_date")]
    
    if overdue:
        lines.append("- OVERDUE:")
        for t in overdue[:5]:
            lines.append(f"  - {t['content']} (was due {t['due']})")
    
    if due_today:
        lines.append("- Due today:")
        for t in due_today[:5]:
            lines.append(f"  - {t['content']}")
    
    if upcoming:
        lines.append("- Upcoming:")
        for t in upcoming[:5]:
            lines.append(f"  - {t['content']} (due {t['due']})")
    
    if no_date and len(lines) < 8:
        lines.append("- No due date:")
        for t in no_date[:3]:
            lines.append(f"  - {t['content']}")
    
    return "\n".join(lines)


# ============================================================
# GOOGLE CALENDAR (via Google Calendar API)
# ============================================================

GOOGLE_CALENDAR_CREDENTIALS = os.environ.get("GOOGLE_CALENDAR_CREDS", "")

def get_google_calendar_events():
    """Get today's and tomorrow's calendar events.
    Requires OAuth2 setup - will be configured separately."""
    # Placeholder - needs OAuth2 token flow
    return {"error": "Google Calendar not yet configured"}


def get_calendar_summary():
    """Get formatted calendar summary."""
    events = get_google_calendar_events()
    if isinstance(events, dict) and "error" in events:
        return ""
    return ""


# ============================================================
# GMAIL (via Gmail API)
# ============================================================

def get_recent_emails():
    """Get recent unread emails.
    Requires OAuth2 setup - will be configured separately."""
    return {"error": "Gmail not yet configured"}


def get_email_summary():
    """Get formatted email summary."""
    emails = get_recent_emails()
    if isinstance(emails, dict) and "error" in emails:
        return ""
    return ""


# ============================================================
# UPDATED COMBINED CONTEXT
# ============================================================

def get_data_context():
    """Get all data source context combined for Alfred's system prompt."""
    parts = []
    
    # Time is always fresh
    time_ctx = get_time_summary()
    if time_ctx:
        parts.append(time_ctx)
    
    # Cache weather for 5 min
    weather_ctx = _get_cached('weather', get_weather_summary)
    if weather_ctx:
        parts.append(weather_ctx)
    
    # Cache todoist for 5 min
    todoist_ctx = _get_cached('todoist', get_todoist_summary)
    if todoist_ctx:
        parts.append(todoist_ctx)
    
    return "\n\n".join(parts)
