"""Alfred's commitment tracker, detects and tracks promises made in conversation."""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from database import get_db
from config import DB_PATH
from local_model import classify, generate


def add_commitment(content, person=None, due_date=None):
    """Add a tracked commitment."""
    conn = get_db(DB_PATH)
    conn.execute(
        "INSERT INTO commitments (content, person, due_date) VALUES (?, ?, ?)",
        (content, person, due_date)
    )
    conn.commit()
    conn.close()


def get_pending_commitments():
    """Get all pending commitments."""
    conn = get_db(DB_PATH)
    rows = conn.execute(
        "SELECT * FROM commitments WHERE status = 'pending' ORDER BY ts_created"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def complete_commitment(commitment_id):
    """Mark a commitment as complete."""
    conn = get_db(DB_PATH)
    conn.execute(
        "UPDATE commitments SET status = 'completed', ts_completed = datetime('now') WHERE id = ?",
        (commitment_id,)
    )
    conn.commit()
    conn.close()


def check_for_commitments(text):
    """Analyze text for commitment language and extract any commitments."""
    category = classify(text, ["commitment", "question", "observation", "goal", "other"])
    if category != "commitment":
        return None
    
    result = generate(
        f"Extract the commitment from this text. Who is it to? When is it due? "
        f"Return JSON with keys: commitment, person, due_date (or null)\n\nText: {text}",
        max_tokens=200
    )
    
    try:
        start = result.find("{")
        end = result.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(result[start:end])
            add_commitment(
                content=data.get("commitment", text),
                person=data.get("person"),
                due_date=data.get("due_date")
            )
            return data
    except (json.JSONDecodeError, Exception):
        pass
    
    return None


def get_commitment_summary():
    """Get a formatted summary of pending commitments for Alfred's context."""
    pending = get_pending_commitments()
    if not pending:
        return ""
    
    lines = ["## Pending Commitments"]
    for c in pending:
        due = f" (due: {c['due_date']})" if c.get('due_date') else ""
        person = f" to {c['person']}" if c.get('person') else ""
        lines.append(f"- {c['content']}{person}{due}")
    
    return "\n".join(lines)


if __name__ == "__main__":
    print("Pending commitments:")
    for c in get_pending_commitments():
        print(f"  [{c['id']}] {c['content']}")
