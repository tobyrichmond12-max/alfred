"""Alfred conversation engine, the core chat loop with memory."""
import anthropic
import json
import os
from datetime import datetime
# Load .env file
import os as _os
_env_path = '/mnt/nvme/alfred/.env'
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                _os.environ.setdefault(_k.strip(), _v.strip())

from personality import get_system_prompt
from config import CLAUDE_MODEL, DB_PATH
from database import get_db, init_databases
from memory import get_context_package, ingest_conversation
from commitments import get_commitment_summary, check_for_commitments
from goals import get_goals_summary
from data_sources import get_data_context
from nudge import get_activity_summary

client = anthropic.Anthropic()

# Model routing, pick the right model based on complexity
COMPLEXITY_KEYWORDS = {
    'sonnet': [
        'analyze', 'compare', 'explain why', 'help me think', 'plan', 'strategy',
        'write me', 'draft', 'review', 'debug', 'architect', 'design',
        'research', 'deep dive', 'pros and cons', 'tradeoffs', 'evaluate',
        'brainstorm', 'create', 'build', 'complex', 'detailed', 'essay',
        'code', 'script', 'implement', 'refactor'
    ]
}

def pick_model(message):
    """Route to the right model based on message complexity.
    Haiku for quick stuff, Sonnet for heavy thinking."""
    from config import CLAUDE_MODEL, CLAUDE_HAIKU
    
    msg_lower = message.lower()
    
    # Long messages likely need more thinking
    if len(message) > 500:
        return CLAUDE_MODEL
    
    # Check for complexity keywords
    for keyword in COMPLEXITY_KEYWORDS['sonnet']:
        if keyword in msg_lower:
            return CLAUDE_MODEL
    
    # Questions with ? are usually simple
    # Short messages are usually simple
    # Default to Haiku for speed
    return CLAUDE_HAIKU

# Conversation history (in-memory for session, persisted to DB)
conversation_history = []
current_session_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def _load_recent_history(max_turns=20):
    """Load recent conversation history from DB so context persists across restarts."""
    global conversation_history, current_session_id
    try:
        conn = get_db(DB_PATH)
        rows = conn.execute(
            "SELECT role, content FROM conversations ORDER BY id DESC LIMIT ?",
            (max_turns * 2,)
        ).fetchall()
        conn.close()
        if rows:
            conversation_history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
            current_session_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    except Exception:
        pass


# Load history on module import
_load_recent_history()


def _get_core_profile():
    """Load the current core profile from the database."""
    try:
        conn = get_db(DB_PATH)
        row = conn.execute("SELECT content FROM core_profile WHERE id = 1").fetchone()
        conn.close()
        return row["content"] if row else None
    except Exception:
        return None


def _persist_message(role, content):
    """Save a message to the database."""
    try:
        conn = get_db(DB_PATH)
        conn.execute(
            "INSERT INTO conversations (role, content, session_id) VALUES (?, ?, ?)",
            (role, content, current_session_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Warning: could not persist message: {e}")


def chat(user_message, core_profile=None):
    """Send a message to Alfred and get a response."""
    
    # Get memory context relevant to this message
    memory_context = get_context_package(user_message)
    
    # Build core profile
    profile = core_profile or _get_core_profile()
    if memory_context:
        profile = (profile or "") + "\n\n" + memory_context
    
    # Add pending commitments
    commitment_context = get_commitment_summary()
    if commitment_context:
        profile = (profile or "") + "\n\n" + commitment_context
    
    # Add active goals
    goals_context = get_goals_summary()
    if goals_context:
        profile = (profile or "") + "\n\n" + goals_context
    
    # Add real-time data (weather, time, todoist)
    data_context = get_data_context()
    if data_context:
        profile = (profile or "") + "\n\n" + data_context
    
    # Add recent activity context
    activity_context = get_activity_summary()
    if activity_context:
        profile = (profile or "") + "\n\n" + activity_context
    
    system_prompt = get_system_prompt(profile)
    
    # Add timestamp context
    now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    contextualized_message = f"[{now}] {user_message}"
    
    conversation_history.append({
        "role": "user",
        "content": contextualized_message
    })
    _persist_message("user", user_message)
    
    # Keep last 20 turns to manage context window
    recent_history = conversation_history[-40:]
    
    selected_model = pick_model(user_message)
    
    response = client.messages.create(
        model=selected_model,
        max_tokens=1024,
        system=system_prompt,
        messages=recent_history
    )
    
    assistant_message = response.content[0].text
    conversation_history.append({
        "role": "assistant",
        "content": assistant_message
    })
    _persist_message("assistant", assistant_message)
    
    # Background: extract facts and commitments from the user's message
    try:
        ingest_conversation(user_message, session_id=current_session_id)
        check_for_commitments(user_message)
    except Exception:
        pass  # Don't let memory/commitment failures break conversation
    
    return assistant_message


def fast_chat(user_message):
    """Fast response using Haiku, for iOS Shortcut where speed matters."""
    from config import CLAUDE_HAIKU
    
    # Lighter context, skip memory search for speed
    profile = _get_core_profile()
    
    # Add commitments and goals (cached, fast)
    commitment_context = get_commitment_summary()
    if commitment_context:
        profile = (profile or "") + "\n\n" + commitment_context
    
    goals_context = get_goals_summary()
    if goals_context:
        profile = (profile or "") + "\n\n" + goals_context
    
    # Add cached data context (weather, todoist)
    data_context = get_data_context()
    if data_context:
        profile = (profile or "") + "\n\n" + data_context
    
    system_prompt = get_system_prompt(profile)
    
    now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    contextualized_message = f"[{now}] {user_message}"
    
    conversation_history.append({"role": "user", "content": contextualized_message})
    _persist_message("user", user_message)
    
    recent_history = conversation_history[-20:]
    
    selected_model = pick_model(user_message)
    
    response = client.messages.create(
        model=selected_model,
        max_tokens=512 if 'haiku' in selected_model else 1024,
        system=system_prompt,
        messages=recent_history
    )
    
    assistant_message = response.content[0].text
    conversation_history.append({"role": "assistant", "content": assistant_message})
    _persist_message("assistant", assistant_message)
    
    # Background memory ingestion
    try:
        ingest_conversation(user_message, session_id=current_session_id)
        check_for_commitments(user_message)
    except Exception:
        pass
    
    return assistant_message


def reset():
    """Clear conversation history and start a new session."""
    global current_session_id
    conversation_history.clear()
    current_session_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")


if __name__ == "__main__":
    init_databases()
    print("Alfred is ready. Type 'quit' to exit.\n")
    while True:
        try:
            user_input = input("You: ").strip()
            if user_input.lower() in ("quit", "exit", "q"):
                print("\nAlfred: Very good, sir. I shall be here when you need me.")
                break
            if not user_input:
                continue
            response = chat(user_input)
            print(f"\nAlfred: {response}\n")
        except KeyboardInterrupt:
            print("\n\nAlfred: Good evening, sir.")
            break
        except Exception as e:
            print(f"\nError: {e}\n")
