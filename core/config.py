"""Alfred configuration."""
import os

# Paths
ALFRED_HOME = "/mnt/nvme/alfred"
CORE_DIR = os.path.join(ALFRED_HOME, "core")
DATA_DIR = os.path.join(ALFRED_HOME, "data")
VAULT_DIR = os.path.join(ALFRED_HOME, "vault")
LOGS_DIR = os.path.join(ALFRED_HOME, "logs")
AUDIO_DIR = os.path.join(ALFRED_HOME, "audio")

# Models
LOCAL_MODEL = "gemma3:4b"
EMBED_MODEL = "nomic-embed-text"
OLLAMA_BASE_URL = "http://localhost:11434"

# Claude
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_HAIKU = "claude-haiku-4-5-20251001"

# Database
DB_PATH = os.path.join(DATA_DIR, "alfred.db")
MEMORY_DB_PATH = os.path.join(DATA_DIR, "memory.db")

# Audio
AUDIO_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1
