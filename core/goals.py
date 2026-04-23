"""Alfred's goal tracking system."""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from database import get_db
from config import DB_PATH


def add_goal(title, description=None, category=None, target_date=None):
    """Add a new goal."""
    conn = get_db(DB_PATH)
    conn.execute(
        "INSERT INTO goals (title, description, category, target_date) VALUES (?, ?, ?, ?)",
        (title, description, category, target_date)
    )
    conn.commit()
    conn.close()


def get_active_goals():
    """Get all active goals."""
    conn = get_db(DB_PATH)
    rows = conn.execute(
        "SELECT * FROM goals WHERE status = 'active' ORDER BY ts_created"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_goal_progress(goal_id, note):
    """Add a progress note to a goal."""
    conn = get_db(DB_PATH)
    row = conn.execute("SELECT progress_notes FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if row:
        notes = json.loads(row["progress_notes"])
        notes.append({"ts": datetime.utcnow().isoformat(), "note": note})
        conn.execute(
            "UPDATE goals SET progress_notes = ? WHERE id = ?",
            (json.dumps(notes), goal_id)
        )
        conn.commit()
    conn.close()


def complete_goal(goal_id):
    """Mark a goal as complete."""
    conn = get_db(DB_PATH)
    conn.execute("UPDATE goals SET status = 'completed' WHERE id = ?", (goal_id,))
    conn.commit()
    conn.close()


def get_goals_summary():
    """Get a formatted summary of active goals for Alfred's context."""
    goals = get_active_goals()
    if not goals:
        return ""
    
    lines = ["## Active Goals"]
    for g in goals:
        target = f" (target: {g['target_date']})" if g.get('target_date') else ""
        cat = f" [{g['category']}]" if g.get('category') else ""
        notes = json.loads(g.get('progress_notes', '[]'))
        latest = f", latest: {notes[-1]['note'][:60]}" if notes else ""
        lines.append(f"- {g['title']}{cat}{target}{latest}")
    
    return "\n".join(lines)


if __name__ == "__main__":
    print("Active goals:")
    for g in get_active_goals():
        print(f"  [{g['id']}] {g['title']}")
