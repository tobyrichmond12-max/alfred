"""Alfred MCP Server, exposes Jetson capabilities to Claude."""
import asyncio
import json
import os
import subprocess
import sys
from typing import Annotated
from datetime import datetime

from pydantic import Field
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(__file__))
from config import (ALFRED_HOME, DATA_DIR, VAULT_DIR, LOGS_DIR,
                    LOCAL_MODEL, OLLAMA_BASE_URL)

mcp = FastMCP("Alfred")


def _run_local_model(prompt, system=None, max_tokens=500):
    """Run inference on the local Qwen3-4B model via Ollama."""
    import urllib.request
    
    payload = {
        "model": LOCAL_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens}
    }
    if system:
        payload["system"] = system
    
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode())
    return result.get("response", "")


def _log_tool_call(tool_name, args_summary, result_summary):
    """Append to audit log."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    log_path = os.path.join(LOGS_DIR, "tool_audit.jsonl")
    entry = {
        "ts": datetime.utcnow().isoformat(),
        "tool": tool_name,
        "args": args_summary,
        "result_len": len(result_summary),
        "result_preview": result_summary[:200]
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


@mcp.tool()
async def query_vault(
    query: Annotated[str, Field(description="What to look up in private data")],
    scope: Annotated[str, Field(description="Scope: calendar, email, health, goals, relationships, all")] = "all",
    max_results: Annotated[int, Field(description="Max items to return")] = 5,
) -> str:
    """Query the private vault. Returns sanitized summaries, Claude never sees raw private data.
    The local model reads the vault and returns a summary with placeholder tokens for sensitive info."""
    
    # For now, check if vault has any data files
    vault_files = []
    if os.path.exists(VAULT_DIR):
        for f in os.listdir(VAULT_DIR):
            vault_files.append(f)
    
    if not vault_files:
        return "The vault is currently empty. No private data has been ingested yet. Sir's data sources (calendar, email, health) have not been connected."
    
    # When vault has data, the local model will summarize it here
    system = "You are a privacy-preserving summarizer. Read the provided data and return a summary. Replace all names with <PERSON_A>, <PERSON_B> etc. Replace specific dates with relative references. Never include raw personal data."
    
    result = await asyncio.get_event_loop().run_in_executor(
        None, _run_local_model,
        f"Summarize the following vault data relevant to: {query}\nScope: {scope}\nMax results: {max_results}",
        system
    )
    
    _log_tool_call("query_vault", f"query={query}, scope={scope}", result)
    return result


@mcp.tool()
async def run_local_inference(
    prompt: Annotated[str, Field(description="Prompt for the local model")],
    system: Annotated[str, Field(description="System prompt")] = None,
    max_tokens: Annotated[int, Field(description="Max tokens to generate")] = 500,
) -> str:
    """Run inference on the local Qwen3-4B model. Use for classification, summarization,
    or any task that doesn't need Claude-level reasoning."""
    
    result = await asyncio.get_event_loop().run_in_executor(
        None, _run_local_model, prompt, system, max_tokens
    )
    
    _log_tool_call("run_local_inference", f"prompt={prompt[:100]}", result)
    return result


@mcp.tool()
async def store_observation(
    content: Annotated[str, Field(description="The observation to store")],
    tags: Annotated[str, Field(description="Comma-separated tags")] = "",
    sensitivity: Annotated[str, Field(description="low, medium, high")] = "low",
) -> str:
    """Store an observation about the user or their world. These feed into Alfred's memory system."""
    
    os.makedirs(DATA_DIR, exist_ok=True)
    obs_path = os.path.join(DATA_DIR, "observations.jsonl")
    
    entry = {
        "ts": datetime.utcnow().isoformat(),
        "content": content,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "sensitivity": sensitivity
    }
    
    with open(obs_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    
    _log_tool_call("store_observation", f"tags={tags}, sensitivity={sensitivity}", content)
    return f"Observation stored: {content[:100]}..."


@mcp.tool()
async def act_with_confirmation(
    action_type: Annotated[str, Field(description="Type: send_email, send_message, create_file, run_command, other")],
    params: Annotated[str, Field(description="JSON string of action parameters")],
    confirmation_prompt: Annotated[str, Field(description="What to ask the user for confirmation")],
) -> str:
    """Request confirmation before taking an irreversible action. 
    Returns the confirmation prompt, the user must approve before execution."""
    
    _log_tool_call("act_with_confirmation", f"type={action_type}", confirmation_prompt)
    
    return json.dumps({
        "status": "awaiting_confirmation",
        "action_type": action_type,
        "params": json.loads(params) if params.startswith("{") else params,
        "confirmation_prompt": confirmation_prompt,
        "message": f"Sir, I require your approval: {confirmation_prompt}"
    }, indent=2)


@mcp.tool()
async def system_status() -> str:
    """Get current system status, memory, GPU, disk, running services."""
    
    checks = {}
    
    # Memory
    result = subprocess.run(["free", "-h"], capture_output=True, text=True)
    checks["memory"] = result.stdout.strip()
    
    # Disk
    result = subprocess.run(["df", "-h", "/mnt/nvme"], capture_output=True, text=True)
    checks["disk"] = result.stdout.strip()
    
    # GPU / tegrastats snapshot
    try:
        result = subprocess.run(["tegrastats", "--interval", "1000", "--count", "1"],
                              capture_output=True, text=True, timeout=5)
        checks["gpu"] = result.stdout.strip()
    except Exception:
        checks["gpu"] = "tegrastats unavailable"
    
    # Ollama
    try:
        import urllib.request
        resp = urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=3)
        models = json.loads(resp.read().decode())
        checks["ollama"] = f"running, {len(models.get('models', []))} models loaded"
    except Exception as e:
        checks["ollama"] = f"error: {e}"
    
    # Uptime
    result = subprocess.run(["uptime"], capture_output=True, text=True)
    checks["uptime"] = result.stdout.strip()
    
    _log_tool_call("system_status", "", json.dumps(checks)[:200])
    return json.dumps(checks, indent=2)


@mcp.tool()
async def create_todoist_task(
    content: Annotated[str, Field(description="Task title, e.g. 'Pick up groceries'")],
    due_string: Annotated[str, Field(description="Natural language due date: 'tomorrow', 'tonight at 6pm', 'next Monday', etc.")] = None,
    priority: Annotated[int, Field(description="1=normal, 2=medium, 3=high, 4=urgent")] = 1,
) -> str:
    """Create a new task in Todoist. Todoist parses the due_string naturally so pass it exactly as spoken."""
    sys.path.insert(0, os.path.dirname(__file__))
    from todoist import create_task

    loop = asyncio.get_event_loop()
    task = await loop.run_in_executor(
        None, lambda: create_task(content, due_string, priority)
    )

    result = f"Task created: '{task['content']}'"
    if task.get("due"):
        due = task["due"]
        result += f", due {due.get('string') or due.get('date', '')}"

    _log_tool_call("create_todoist_task", f"content={content}, due={due_string}", result)
    return result


if __name__ == "__main__":
    mcp.run(transport="stdio")
