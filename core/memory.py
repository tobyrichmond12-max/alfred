"""Alfred's memory system, three-tier memory with vector search."""
import json
import struct
import os
from datetime import datetime
from database import get_db
from local_model import embed, extract_facts, classify
from config import MEMORY_DB_PATH, DB_PATH


def _blob_to_floats(blob):
    """Convert a blob of floats back to a list."""
    n = len(blob) // 4
    return list(struct.unpack(f'{n}f', blob))


def _floats_to_blob(floats):
    """Convert a list of floats to a blob for storage."""
    return struct.pack(f'{len(floats)}f', *floats)


def _cosine_similarity(a, b):
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def store_memory(content, memory_type="observation", tags=None, importance=0.5, source_episode_ids=None):
    """Store a memory with its embedding."""
    conn = get_db(MEMORY_DB_PATH)
    
    # Generate embedding
    embeddings = embed(content)
    emb_blob = _floats_to_blob(embeddings[0]) if embeddings else None
    
    conn.execute("""
        INSERT INTO memories (content, memory_type, tags, importance, embedding, source_episode_ids)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        content,
        memory_type,
        json.dumps(tags or []),
        importance,
        emb_blob,
        json.dumps(source_episode_ids or [])
    ))
    conn.commit()
    mem_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return mem_id


def search_memories(query, top_k=8, memory_type=None):
    """Search memories using vector similarity + recency + importance scoring."""
    conn = get_db(MEMORY_DB_PATH)
    
    # Get query embedding
    query_emb = embed(query)
    if not query_emb:
        return []
    query_vec = query_emb[0]
    
    # Fetch all memories with embeddings
    where_clause = "WHERE embedding IS NOT NULL"
    if memory_type:
        where_clause += f" AND memory_type = '{memory_type}'"
    
    rows = conn.execute(f"""
        SELECT id, content, memory_type, tags, importance, access_count,
               ts_created, ts_last_accessed, embedding
        FROM memories {where_clause}
    """).fetchall()
    
    if not rows:
        conn.close()
        return []
    
    # Score each memory
    now = datetime.utcnow()
    scored = []
    for row in rows:
        mem_vec = _blob_to_floats(row["embedding"])
        relevance = _cosine_similarity(query_vec, mem_vec)
        
        # Recency decay: 0.995 ^ hours_since_creation
        created = datetime.fromisoformat(row["ts_created"])
        hours_since = (now - created).total_seconds() / 3600
        recency = 0.995 ** hours_since
        
        importance = row["importance"]
        access_freq = min(row["access_count"] / 10, 1.0)  # Normalize to 0-1
        
        # Combined score
        score = 0.4 * relevance + 0.3 * recency + 0.2 * importance + 0.1 * access_freq
        
        scored.append({
            "id": row["id"],
            "content": row["content"],
            "memory_type": row["memory_type"],
            "tags": json.loads(row["tags"]),
            "importance": importance,
            "score": score,
            "relevance": relevance,
            "ts_created": row["ts_created"]
        })
    
    # Sort by score, return top_k
    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:top_k]
    
    # Update access counts
    for item in top:
        conn.execute("""
            UPDATE memories SET access_count = access_count + 1,
                               ts_last_accessed = datetime('now')
            WHERE id = ?
        """, (item["id"],))
    conn.commit()
    conn.close()
    
    return top


def ingest_conversation(text, session_id=None):
    """Extract facts from conversation text and store as memories."""
    facts = extract_facts(text)
    stored = []
    
    for fact in facts:
        fact_text = fact.get("fact", "")
        fact_type = fact.get("type", "observation")
        confidence = fact.get("confidence", 0.5)
        
        if fact_text and confidence > 0.3:
            mem_id = store_memory(
                content=fact_text,
                memory_type=fact_type,
                importance=confidence,
                source_episode_ids=[session_id] if session_id else []
            )
            stored.append({"id": mem_id, "fact": fact_text, "type": fact_type})
    
    return stored


def get_context_package(query, max_memories=8):
    """Assemble the context package for a Claude API call.
    Returns relevant memories formatted for injection into the system prompt."""
    
    memories = search_memories(query, top_k=max_memories)
    
    if not memories:
        return ""
    
    lines = ["## Relevant Memories"]
    for m in memories:
        lines.append(f"- [{m['memory_type']}] {m['content']} (from {m['ts_created'][:10]})")
    
    return "\n".join(lines)


if __name__ == "__main__":
    print("Testing memory system...")
    
    # Store some test memories
    id1 = store_memory("the user wants to exercise 4 times a week", memory_type="goal", importance=0.8)
    id2 = store_memory("<contact-name-b> is working on the partnership project", memory_type="relationship", importance=0.6)
    id3 = store_memory("the user committed to sending a proposal by Friday", memory_type="commitment", importance=0.9)
    print(f"Stored memories: {id1}, {id2}, {id3}")
    
    # Search
    results = search_memories("What did the user promise to do?")
    print(f"\nSearch for 'What did the user promise to do?':")
    for r in results:
        print(f"  [{r['score']:.3f}] {r['content']}")
    
    # Context package
    ctx = get_context_package("exercise goals")
    print(f"\nContext package:\n{ctx}")
