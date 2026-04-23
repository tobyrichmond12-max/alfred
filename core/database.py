"""Alfred's database layer, SQLite with WAL mode."""
import sqlite3
import os
import json
from datetime import datetime
from config import DB_PATH, MEMORY_DB_PATH, DATA_DIR


def get_db(path=None):
    """Get a SQLite connection with WAL mode."""
    db_path = path or DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_databases():
    """Create all tables if they don't exist."""
    
    # Main database
    conn = get_db(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now')),
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            session_id TEXT,
            metadata TEXT DEFAULT '{}'
        );
        
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now')),
            content TEXT NOT NULL,
            tags TEXT DEFAULT '[]',
            sensitivity TEXT DEFAULT 'low',
            source TEXT DEFAULT 'manual'
        );
        
        CREATE TABLE IF NOT EXISTS commitments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_created TEXT NOT NULL DEFAULT (datetime('now')),
            content TEXT NOT NULL,
            due_date TEXT,
            person TEXT,
            status TEXT DEFAULT 'pending',
            ts_completed TEXT,
            reminder_count INTEGER DEFAULT 0
        );
        
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_created TEXT NOT NULL DEFAULT (datetime('now')),
            title TEXT NOT NULL,
            description TEXT,
            category TEXT,
            target_date TEXT,
            status TEXT DEFAULT 'active',
            progress_notes TEXT DEFAULT '[]'
        );
        
        CREATE TABLE IF NOT EXISTS tool_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now')),
            tool_name TEXT NOT NULL,
            args_hash TEXT,
            result_hash TEXT,
            result_preview TEXT,
            approved INTEGER DEFAULT 1
        );
        
        CREATE TABLE IF NOT EXISTS core_profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            content TEXT NOT NULL,
            ts_updated TEXT NOT NULL DEFAULT (datetime('now'))
        );
        
        INSERT OR IGNORE INTO core_profile (id, content) VALUES (1, 
            'The user is the user. Alfred is still learning about him. Profile will be enriched over time.'
        );
    """)
    conn.commit()
    conn.close()
    
    # Memory database (will hold embeddings and knowledge graph references)
    conn = get_db(MEMORY_DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_created TEXT NOT NULL DEFAULT (datetime('now')),
            content TEXT NOT NULL,
            memory_type TEXT NOT NULL DEFAULT 'observation',
            tags TEXT DEFAULT '[]',
            importance REAL DEFAULT 0.5,
            access_count INTEGER DEFAULT 0,
            ts_last_accessed TEXT,
            embedding BLOB,
            valid_at TEXT,
            invalid_at TEXT,
            source_episode_ids TEXT DEFAULT '[]'
        );
        
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            aliases TEXT DEFAULT '[]',
            properties TEXT DEFAULT '{}',
            ts_created TEXT NOT NULL DEFAULT (datetime('now')),
            ts_updated TEXT NOT NULL DEFAULT (datetime('now'))
        );
        
        CREATE TABLE IF NOT EXISTS relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_entity_id INTEGER NOT NULL,
            target_entity_id INTEGER NOT NULL,
            predicate TEXT NOT NULL,
            properties TEXT DEFAULT '{}',
            valid_at TEXT,
            invalid_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expired_at TEXT,
            episode_ids TEXT DEFAULT '[]',
            FOREIGN KEY (source_entity_id) REFERENCES entities(id),
            FOREIGN KEY (target_entity_id) REFERENCES entities(id)
        );
        
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_start TEXT NOT NULL DEFAULT (datetime('now')),
            ts_end TEXT,
            summary TEXT,
            key_facts TEXT DEFAULT '[]',
            commitments_extracted TEXT DEFAULT '[]'
        );
    """)
    conn.commit()
    conn.close()
    
    print(f"Databases initialized at {DATA_DIR}")


if __name__ == "__main__":
    init_databases()
