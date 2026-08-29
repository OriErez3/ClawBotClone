import sqlite3
import threading
from pathlib import Path

#Anchor the DB next to this file so the bot finds it no matter where it's launched from
DB_PATH = Path(__file__).resolve().parent / "memory.db"
#check_same_thread=False because tools run in worker threads; the lock serializes all
#access since a sqlite connection can't be used by two threads at once
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_lock = threading.Lock()
cursor = conn.cursor()

#sqlite-vec adds vector similarity search to THIS same DB file (an extra virtual table, not a
#separate store). If it can't load (extension missing, or a Python built without extension
#support), the bot still runs fully - the vector functions below just no-op and recall is
#skipped. EMBED_DIM must match the model used in embeddings.py (text-embedding-004 -> 768).
EMBED_DIM = 768
try:
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    VECTOR_ENABLED = True
except Exception as _vec_err:  #ImportError, or AttributeError if the build lacks extension support
    import logging as _logging
    _logging.getLogger(__name__).warning("Vector search disabled (sqlite-vec unavailable): %s", _vec_err)
    VECTOR_ENABLED = False
cursor.execute('''
    CREATE TABLE IF NOT EXISTS conversation (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT,
        message TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
''')
cursor.execute('''
    CREATE TABLE IF NOT EXISTS memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT UNIQUE,
        value TEXT
    )
''')
cursor.execute('''
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
''')
cursor.execute('''
    CREATE TABLE IF NOT EXISTS scheduled_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        task TEXT,
        due_timestamp REAL,
        status TEXT DEFAULT 'pending'
    )
''')
#Migration for databases created before the status column existed
try:
    cursor.execute("ALTER TABLE scheduled_tasks ADD COLUMN status TEXT DEFAULT 'pending'")
except sqlite3.OperationalError:
    pass  #column already exists
cursor.execute('''
    CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT
    )
''')
#Migration for databases created before conversations existed: add conversation_id to
#existing messages (DEFAULT 1 backfills old rows) and register conversation 1 for them
try:
    cursor.execute("ALTER TABLE conversation ADD COLUMN conversation_id INTEGER DEFAULT 1")
except sqlite3.OperationalError:
    pass  #column already exists
try:
    cursor.execute("ALTER TABLE conversation ADD COLUMN tool_log TEXT DEFAULT NULL")
except sqlite3.OperationalError:
    pass  #column already exists
cursor.execute("INSERT OR IGNORE INTO conversations (id, title) VALUES (1, 'Conversation 1')")
conn.commit()

#Vector store: one metadata table holding the raw text + provenance, and a parallel sqlite-vec
#virtual table holding the embeddings (rowid = embeddings.id). Everything embeddable - messages,
#memory facts, emails - lives here in one searchable space, keyed by `source`. `ref` is an
#optional external id (e.g. a gmail message id) so the same item isn't embedded twice.
if VECTOR_ENABLED:
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            text TEXT,
            ref TEXT,
            created REAL DEFAULT (unixepoch())
        )
    ''')
    cursor.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0(embedding float[{EMBED_DIM}])")
    conn.commit()

def add_embedding(source: str, text: str, embedding: list, ref: str | None = None) -> None:
    """Stores a text and its vector for later semantic recall. `source` tags provenance
    ('message' | 'memory' | 'email'); `ref` is an optional external id used for dedup.
    No-ops if the vector extension isn't available or the embedding is empty."""
    if not VECTOR_ENABLED or not embedding:
        return
    with _lock:
        cursor.execute("INSERT INTO embeddings (source, text, ref) VALUES (?, ?, ?)", (source, text, ref))
        row_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO vec_embeddings (rowid, embedding) VALUES (?, ?)",
            (row_id, sqlite_vec.serialize_float32(embedding)),
        )
        conn.commit()

def embedding_ref_exists(ref: str) -> bool:
    """True if an item with this external ref was already embedded (dedup for e.g. emails)."""
    if not VECTOR_ENABLED:
        return False
    with _lock:
        cursor.execute("SELECT 1 FROM embeddings WHERE ref = ?", (ref,))
        return cursor.fetchone() is not None

def search_embeddings(embedding: list, k: int = 5, sources: list | None = None) -> list:
    """Returns up to k stored texts most similar to `embedding`, nearest first, as
    [{'source', 'text', 'distance'}]. `sources` optionally restricts which kinds to return.
    Empty list if vectors are disabled or the query embedding is empty."""
    if not VECTOR_ENABLED or not embedding:
        return []
    #sqlite-vec KNN wants the MATCH+LIMIT isolated; join to the metadata table around it.
    #Over-fetch when filtering by source so the post-filter can still return up to k.
    fetch = k * 4 if sources else k
    with _lock:
        rows = cursor.execute(
            "SELECT e.source, e.text, v.distance FROM ("
            "  SELECT rowid, distance FROM vec_embeddings WHERE embedding MATCH ? ORDER BY distance LIMIT ?"
            ") v JOIN embeddings e ON e.id = v.rowid ORDER BY v.distance",
            (sqlite_vec.serialize_float32(embedding), fetch),
        ).fetchall()
    results = [{"source": r[0], "text": r[1], "distance": r[2]} for r in rows]
    if sources:
        results = [r for r in results if r["source"] in sources]
    return results[:k]

def clear_conversation(conversation_id: int):
    with _lock:
        cursor.execute("DELETE FROM conversation WHERE conversation_id = ?", (conversation_id,))
        conn.commit()

def add_to_conversation(role: str, message: str, conversation_id: int, tool_log: str = ""):
    with _lock:
        cursor.execute('''
            INSERT INTO conversation (role, message, conversation_id, tool_log) VALUES (?, ?, ?, ?)
        ''', (role, message, conversation_id, tool_log or None))
        conn.commit()

def read_conversation(limit: int, conversation_id: int):
    with _lock:
        cursor.execute('''
            SELECT role, message FROM conversation
            WHERE conversation_id = ?
            ORDER BY id DESC LIMIT ?
        ''', (conversation_id, limit))
        rows = cursor.fetchall()
    return [{"role": row[0], "parts": [row[1]]} for row in reversed(rows)]

def add_to_memory(key: str, value: str):
    with _lock:
        cursor.execute('''
            INSERT INTO memory (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        ''', (key, value))
        conn.commit()
def read_memory():
    with _lock:
        cursor.execute('''
            SELECT key, value FROM memory ''')
        rows = cursor.fetchall()
    if not rows:
        return ''
    return "\n".join(f"{row[0]}: {row[1]}" for row in rows)

def set_setting(key: str, value: str):
    with _lock:
        cursor.execute('''
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        ''', (key, value))
        conn.commit()

def get_setting(key: str) -> str | None:
    with _lock:
        cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
        row = cursor.fetchone()
    return row[0] if row else None

def delete_memory(key: str) -> bool:
    with _lock:
        cursor.execute('SELECT key FROM memory WHERE key = ?', (key,))
        if cursor.fetchone():
            cursor.execute('DELETE FROM memory WHERE key = ?', (key,))
            conn.commit()
            return True
    return False

def add_scheduled_task(chat_id: str, task: str, due_timestamp: float):
    with _lock:
        cursor.execute('''
            INSERT INTO scheduled_tasks (chat_id, task, due_timestamp, status) VALUES (?, ?, ?, 'pending')
        ''', (chat_id, task, due_timestamp))
        conn.commit()

def get_due_tasks(now_timestamp: float):
    with _lock:
        cursor.execute("SELECT id, chat_id, task FROM scheduled_tasks WHERE due_timestamp <= ? AND status = 'pending'", (now_timestamp,))
        rows = cursor.fetchall()
    return [{"id": row[0], "chat_id": row[1], "task": row[2]} for row in rows]

def mark_task_running(task_id: int):
    with _lock:
        cursor.execute("UPDATE scheduled_tasks SET status = 'running' WHERE id = ?", (task_id,))
        conn.commit()

def reset_running_tasks() -> int:
    """Re-queues tasks left in 'running' by a crash so they aren't silently lost.
    Returns how many were re-queued (a re-queued task may run twice if the crash
    happened after the action but before cleanup)."""
    with _lock:
        cursor.execute("UPDATE scheduled_tasks SET status = 'pending' WHERE status = 'running'")
        conn.commit()
        return cursor.rowcount

def delete_scheduled_task(task_id: int):
    with _lock:
        cursor.execute('DELETE FROM scheduled_tasks WHERE id = ?', (task_id,))
        conn.commit()

def get_active_conversation_id() -> int:
    value = get_setting("active_conversation_id")
    return int(value) if value else 1

def set_active_conversation_id(conversation_id: int) -> None:
    set_setting("active_conversation_id", str(conversation_id))

def create_conversation() -> int:
    with _lock:
        cursor.execute("INSERT INTO conversations (title) VALUES (NULL)")
        new_id = cursor.lastrowid
        cursor.execute("UPDATE conversations SET title = ? WHERE id = ?", (f"Conversation {new_id}", new_id))
        conn.commit()
    return new_id #type: ignore

def list_conversations():
    with _lock:
        cursor.execute("SELECT id, title FROM conversations ORDER BY id")
        rows = cursor.fetchall()
    return [{"id": row[0], "title": row[1]} for row in rows]

def conversation_exists(conversation_id: int) -> bool:
    with _lock:
        cursor.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,))
        return cursor.fetchone() is not None

def rename_conversation(conversation_id: int, title: str) -> None:
    with _lock:
        cursor.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id))
        conn.commit()

def get_tool_logs(conversation_id: int, limit: int = 30) -> list[str]:
    with _lock:
        cursor.execute('''
            SELECT tool_log FROM conversation
            WHERE conversation_id = ? AND role = 'model' AND tool_log IS NOT NULL
            ORDER BY id DESC LIMIT ?
        ''', (conversation_id, limit))
        rows = cursor.fetchall()
    return [r[0] for r in reversed(rows)]
