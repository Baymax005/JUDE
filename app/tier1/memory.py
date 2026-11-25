from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.config import MEMORY_DB_PATH

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "did", "do", "for", "from",
    "have", "hey", "how", "i", "in", "is", "it", "jude", "me", "my", "of",
    "on", "or", "please", "tell", "that", "the", "this", "to", "uh", "was",
    "what", "when", "where", "which", "who", "you", "your",
}

_SAVE_PREFIX_RE = re.compile(
    r"^(?:(?:hey\s+)?jude[\s,]+)?(?:uh\s+)?(?:please\s+|can\s+you\s+)?"
    r"(?:remember(?:\s+that|\s+to)?|save(?:\s+a\s+reminder|\s+a\s+note|\s+this\s+(?:fact|note|detail|preference|snippet))?(?:\s+that)?|"
    r"store(?:\s+a\s+note|\s+in\s+memory)?(?:\s+that)?|note\s+down(?:\s+that)?|keep\s+in\s+(?:mind|memory)(?:\s+that)?|"
    r"keep\s+(?:on\s+file|this\s+saved|track(?:\s+of)?)(?:\s+that)?|record(?:\s+that)?|log(?:\s+this\s+fact|\s+that)?|"
    r"don'?t\s+(?:let\s+me\s+)?forget(?:\s+that)?|hold\s+onto\s+this(?:\s+detail)?(?:\s+for\s+me)?|"
    r"add\s+to\s+memory|file\s+away(?:\s+that)?|lock\s+in(?:\s+the\s+fact)?(?:\s+that)?|"
    r"mark\s+down(?:\s+that)?|put\s+it\s+in\s+your\s+notes(?:\s+that)?)\s*[:,-]?\s*",
    flags=re.IGNORECASE,
)

_RECALL_PREFIX_RE = re.compile(
    r"^(?:(?:hey\s+)?jude[\s,]+)?(?:uh\s+)?(?:please\s+|can\s+you\s+)?"
    r"(?:recall|remind\s+me(?:\s+of)?|look\s+up|check\s+(?:your\s+memory|my\s+(?:saved\s+)?(?:notes|memory|facts|preferences))(?:\s+for)?|"
    r"tell\s+me(?:\s+about)?|pull\s+(?:up|my\s+stored\s+(?:note|reminder))(?:\s+about)?|"
    r"read\s+back(?:\s+my\s+saved\s+note)?(?:\s+about)?|do\s+you\s+remember|retrieve(?:\s+my\s+saved\s+note)?(?:\s+on)?|"
    r"search\s+my\s+local\s+memory(?:\s+for)?|find)\s*[:,-]?\s*",
    flags=re.IGNORECASE,
)

_DELETE_PREFIX_RE = re.compile(
    r"^(?:(?:hey\s+)?jude[\s,]+)?(?:uh\s+)?(?:please\s+|can\s+you\s+)?"
    r"(?:forget(?:\s+that|\s+about)?|delete|remove|erase|clear)\s+"
    r"(?:(?:my|the)\s+(?:saved\s+)?)?(?:memory|memories|fact|note|notes|entry|info)?\s*"
    r"(?:about|regarding|for|from\s+(?:your\s+|my\s+)?memory)?\s*[:,-]?\s*",
    flags=re.IGNORECASE,
)


def _init_db(conn: sqlite3.Connection) -> bool:
    """Ensure the memories, activity_log, user_preferences tables and optional FTS5 index exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact_text TEXT NOT NULL,
            key_phrase TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            target TEXT NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pref_key TEXT NOT NULL UNIQUE,
            pref_value TEXT NOT NULL,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                fact_text,
                key_phrase,
                content='memories',
                content_rowid='id'
            )
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, fact_text, key_phrase)
                VALUES (new.id, new.fact_text, new.key_phrase);
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, fact_text, key_phrase)
                VALUES ('delete', old.id, old.fact_text, old.key_phrase);
            END
            """
        )
        return True
    except sqlite3.OperationalError:
        return False


def extract_memory_fact(text: str) -> tuple[str, str]:
    """Strip conversational boilerplate and extract (fact_text, key_phrase)."""
    cleaned = " ".join(text.strip().split())
    fact = _SAVE_PREFIX_RE.sub("", cleaned).strip()
    if not fact:
        fact = cleaned

    split_match = re.split(
        r"\s+(?:is|are|as|was|runs\s+on|lives\s+at|meets|opens|closes)\s+",
        fact,
        maxsplit=1,
        flags=re.I,
    )
    if len(split_match) == 2 and len(split_match[0].split()) <= 8:
        key_phrase = split_match[0].strip(" .:,")
    else:
        tokens = [t for t in re.findall(r"[a-zA-Z0-9_.-]+", fact.lower()) if t not in _STOPWORDS]
        key_phrase = " ".join(tokens[:6]) or fact[:48]
    return fact, key_phrase


def save_memory(text: str, db_path: Path = MEMORY_DB_PATH) -> str:
    """Persist a personal fact or note into local SQLite memory."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fact_text, key_phrase = extract_memory_fact(text)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with sqlite3.connect(str(db_path)) as conn:
        _init_db(conn)
        existing = conn.execute(
            "SELECT id FROM memories WHERE lower(fact_text) = lower(?)",
            (fact_text,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE memories SET key_phrase = ?, created_at = ? WHERE id = ?",
                (key_phrase, now_iso, existing[0]),
            )
            conn.commit()
            return f"Updated saved memory: \"{fact_text}\""

        conn.execute(
            "INSERT INTO memories (fact_text, key_phrase, created_at) VALUES (?, ?, ?)",
            (fact_text, key_phrase, now_iso),
        )
        conn.commit()

    return f"Saved to memory: \"{fact_text}\""


def list_memories(db_path: Path = MEMORY_DB_PATH, limit: int = 100) -> list[dict]:
    """Return saved memories ordered newest-first."""
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    with sqlite3.connect(str(db_path)) as conn:
        _init_db(conn)
        rows = conn.execute(
            "SELECT id, fact_text, key_phrase, created_at FROM memories ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "id": int(r[0]),
            "fact_text": str(r[1]),
            "key_phrase": str(r[2]),
            "created_at": str(r[3]),
        }
        for r in rows
    ]


def delete_memory_by_id(mem_id: int, db_path: Path = MEMORY_DB_PATH) -> bool:
    """Delete a single memory row by ID (keeping FTS5 index synced via trigger)."""
    db_path = Path(db_path)
    if not db_path.exists():
        return False
    with sqlite3.connect(str(db_path)) as conn:
        _init_db(conn)
        cur = conn.execute("DELETE FROM memories WHERE id = ?", (int(mem_id),))
        conn.commit()
        return cur.rowcount > 0


def clear_all_memories(db_path: Path = MEMORY_DB_PATH) -> int:
    """Delete all saved memories and return the number of rows removed."""
    db_path = Path(db_path)
    if not db_path.exists():
        return 0
    with sqlite3.connect(str(db_path)) as conn:
        _init_db(conn)
        cur = conn.execute("DELETE FROM memories")
        conn.commit()
        return int(cur.rowcount)


def _search_memory_rows(
    conn: sqlite3.Connection, has_fts: bool, tokens: list[str], limit: int
) -> list[tuple[int, str, str]]:
    matches: list[tuple[int, str, str]] = []
    if has_fts and tokens:
        fts_query = " OR ".join(f'"{t}"*' for t in tokens[:8])
        try:
            matches = conn.execute(
                """
                SELECT m.id, m.fact_text, m.created_at
                FROM memories_fts f
                JOIN memories m ON m.id = f.rowid
                WHERE memories_fts MATCH ?
                ORDER BY bm25(memories_fts), m.id DESC
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            matches = []

    if not matches and tokens:
        all_rows = conn.execute(
            "SELECT id, fact_text, key_phrase, created_at FROM memories ORDER BY id DESC"
        ).fetchall()
        scored: list[tuple[int, int, str, str]] = []
        for row_id, fact, key_phrase, created_at in all_rows:
            hay = f"{fact} {key_phrase}".lower()
            score = sum(1 for tok in tokens if tok in hay)
            if score > 0:
                scored.append((score, row_id, fact, created_at))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        matches = [(row_id, fact, created_at) for _, row_id, fact, created_at in scored[:limit]]

    return matches


def delete_memory_by_query(text: str, db_path: Path = MEMORY_DB_PATH) -> str:
    """Remove matching saved memory entries by natural-language description."""
    db_path = Path(db_path)
    if not db_path.exists():
        return "Your local memory is already empty."

    cleaned = " ".join(text.strip().split())
    low = cleaned.lower()
    if any(p in low for p in ("all memories", "everything in memory", "clear all memory", "wipe memory")):
        return "For safety, open the **Memory** button in the top bar and click **Clear All** to wipe every saved memory."

    stripped = _DELETE_PREFIX_RE.sub("", cleaned).strip(" ?.!,").lower()
    stripped = re.sub(r"\s+from\s+(?:your\s+|my\s+)?memory$", "", stripped, flags=re.I).strip()
    tokens = [
        t for t in re.findall(r"[a-zA-Z0-9_-]+", stripped)
        if t not in _STOPWORDS and len(t) >= 2
    ]
    if not tokens:
        return "Tell me which memory to remove (for example: `Forget my roll number`), or click **Memory** to delete entries manually."

    with sqlite3.connect(str(db_path)) as conn:
        has_fts = _init_db(conn)
        matches = _search_memory_rows(conn, has_fts, tokens, limit=3)
        if not matches:
            return "I couldn't find a matching memory to remove."

        # Delete the best-matching row (or multiple if exact phrase appears in multiple duplicates)
        removed_texts: list[str] = []
        target_id, target_fact, _ = matches[0]
        conn.execute("DELETE FROM memories WHERE id = ?", (target_id,))
        removed_texts.append(target_fact)
        conn.commit()

    return f"Removed from memory: \"{removed_texts[0]}\""


def recall_memory(text: str, db_path: Path = MEMORY_DB_PATH, limit: int = 5) -> str:
    """Search local SQLite memory using FTS5 with token-overlap fallback."""
    db_path = Path(db_path)
    if not db_path.exists():
        return "I don't have any saved memories yet."

    cleaned = " ".join(text.strip().split())
    low = cleaned.lower()
    stripped = _RECALL_PREFIX_RE.sub("", cleaned).strip(" ?.!,").lower()

    with sqlite3.connect(str(db_path)) as conn:
        has_fts = _init_db(conn)

        broad_triggers = (
            "all saved",
            "everything you remember",
            "what is saved",
            "list memories",
            "list my memories",
            "inside your memory",
            "in your memory",
            "my memories",
            "show memory",
            "show my memory",
            "what do you remember",
        )
        if any(p in low for p in broad_triggers):
            rows = conn.execute(
                "SELECT fact_text, created_at FROM memories ORDER BY id DESC LIMIT 20"
            ).fetchall()
            if not rows:
                return "I don't have any saved memories yet."
            lines = [f"- {fact}" for fact, _ in rows]
            return f"Local memory ({len(rows)} item(s)):\n" + "\n".join(lines)

        tokens = [
            t for t in re.findall(r"[a-zA-Z0-9_-]+", stripped)
            if t not in _STOPWORDS and len(t) >= 2
        ]
        if not tokens:
            tokens = [t for t in re.findall(r"[a-zA-Z0-9_-]+", low) if t not in _STOPWORDS]

        matches = _search_memory_rows(conn, has_fts, tokens, limit=limit)

    if not matches:
        return "I couldn't find a matching note in your local memory."
    if len(matches) == 1:
        return f"From memory: {matches[0][1]}"
    return "Here is what I found in memory:\n" + "\n".join(f"- {fact}" for _, fact, _ in matches)


def log_activity(
    action_type: str,
    target: str,
    details: str = "",
    db_path: Path = MEMORY_DB_PATH,
) -> None:
    """Record a desktop or tool interaction into the local SQLite activity_log."""
    clean_action = " ".join(str(action_type).strip().split())[:80]
    clean_target = " ".join(str(target).strip().split())[:200]
    clean_details = " ".join(str(details).strip().split())[:300]
    if not clean_action or not clean_target:
        return
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(str(db_path)) as conn:
        _init_db(conn)
        conn.execute(
            "INSERT INTO activity_log (action_type, target, details, created_at) VALUES (?, ?, ?, ?)",
            (clean_action, clean_target, clean_details, now_iso),
        )
        conn.commit()


def get_frequent_activities(db_path: Path = MEMORY_DB_PATH, limit: int = 5) -> list[dict]:
    """Return top recurring user targets from activity_log ordered by count and recency."""
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    with sqlite3.connect(str(db_path)) as conn:
        _init_db(conn)
        rows = conn.execute(
            """
            SELECT action_type, target, COUNT(*) AS hit_count, MAX(created_at) AS last_used
            FROM activity_log
            GROUP BY action_type, target
            ORDER BY hit_count DESC, last_used DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [
        {
            "action_type": str(r[0]),
            "target": str(r[1]),
            "count": int(r[2]),
            "last_used": str(r[3]),
        }
        for r in rows
    ]


def clear_activity_log(db_path: Path = MEMORY_DB_PATH) -> int:
    """Delete all rows from activity_log and return deleted count."""
    db_path = Path(db_path)
    if not db_path.exists():
        return 0
    with sqlite3.connect(str(db_path)) as conn:
        _init_db(conn)
        cur = conn.execute("DELETE FROM activity_log")
        conn.commit()
        return int(cur.rowcount)


def set_preference(
    pref_key: str,
    pref_value: str,
    source: str = "explicit",
    db_path: Path = MEMORY_DB_PATH,
) -> str:
    """Insert or update a learned/explicit user preference in local SQLite."""
    key = " ".join(str(pref_key).strip().lower().split())[:80]
    val = " ".join(str(pref_value).strip().split())[:300]
    if not key or not val:
        return "Preference key and value cannot be empty."
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(str(db_path)) as conn:
        _init_db(conn)
        conn.execute(
            """
            INSERT INTO user_preferences (pref_key, pref_value, source, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(pref_key) DO UPDATE SET
                pref_value = excluded.pref_value,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (key, val, str(source)[:40], now_iso),
        )
        conn.commit()
    return f"Saved preference `{key}` = `{val}`."


def list_preferences(db_path: Path = MEMORY_DB_PATH, limit: int = 50) -> list[dict]:
    """Return stored user preferences ordered by most recently updated."""
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    with sqlite3.connect(str(db_path)) as conn:
        _init_db(conn)
        rows = conn.execute(
            """
            SELECT id, pref_key, pref_value, source, updated_at
            FROM user_preferences
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [
        {
            "id": int(r[0]),
            "pref_key": str(r[1]),
            "pref_value": str(r[2]),
            "source": str(r[3]),
            "updated_at": str(r[4]),
        }
        for r in rows
    ]


def delete_preference_by_id(pref_id: int, db_path: Path = MEMORY_DB_PATH) -> bool:
    """Delete a single learned/explicit preference row by ID."""
    db_path = Path(db_path)
    if not db_path.exists():
        return False
    with sqlite3.connect(str(db_path)) as conn:
        _init_db(conn)
        cur = conn.execute("DELETE FROM user_preferences WHERE id = ?", (int(pref_id),))
        conn.commit()
        return cur.rowcount > 0


def clear_all_preferences(db_path: Path = MEMORY_DB_PATH) -> int:
    """Delete all stored user preferences and return the count removed."""
    db_path = Path(db_path)
    if not db_path.exists():
        return 0
    with sqlite3.connect(str(db_path)) as conn:
        _init_db(conn)
        cur = conn.execute("DELETE FROM user_preferences")
        conn.commit()
        return int(cur.rowcount)


_WORD_STYLE_CUES: list[tuple[tuple[str, ...], str]] = [
    (("concise", "brief", "shorter", "short", "tighter", "summarize"), "concise"),
    (("formal", "professional", "executive", "polished"), "formal"),
    (("academic", "scholarly", "ieee", "apa", "citation"), "academic"),
    (("bullet", "bullets", "bulleted", "points"), "bulleted"),
    (("simple", "plain", "casual", "easy"), "simple/casual"),
    (("detail", "detailed", "expand", "thorough", "longer"), "detailed"),
]


def infer_word_style_preference(
    instruction: str,
    before_text: str,
    after_text: str,
    db_path: Path = MEMORY_DB_PATH,
) -> str | None:
    """Infer Word editing style preference from an accepted edit and persist it."""
    low = (instruction or "").lower()
    detected: list[str] = []
    for keywords, label in _WORD_STYLE_CUES:
        if any(re.search(rf"\b{re.escape(k)}\b", low) for k in keywords):
            detected.append(label)

    # Secondary structural inference when user shortens or converts to bullets
    if not detected and before_text and after_text:
        bullet_lines = sum(
            1 for ln in after_text.splitlines() if re.match(r"^\s*[-•*]\s+", ln)
        )
        if bullet_lines >= 2:
            detected.append("bulleted")
        elif len(after_text) < max(30, int(len(before_text) * 0.65)):
            detected.append("concise")

    if not detected:
        return None

    style_val = ", ".join(dict.fromkeys(detected))
    set_preference("word_edit_style", style_val, source="inferred_word_edit", db_path=db_path)
    return style_val


def build_user_profile_context(db_path: Path = MEMORY_DB_PATH) -> str:
    """Build a compact <=4-line profile summary of preferences and frequent actions."""
    prefs = list_preferences(db_path=db_path, limit=3)
    habits = get_frequent_activities(db_path=db_path, limit=3)
    lines: list[str] = []
    if prefs:
        pref_str = "; ".join(f"{p['pref_key']}={p['pref_value']}" for p in prefs)
        lines.append(f"Learned Preferences: {pref_str[:220]}")
    if habits:
        habit_str = ", ".join(f"{h['target']} ({h['count']}x)" for h in habits)
        lines.append(f"Frequent Targets: {habit_str[:220]}")
    return "\n".join(lines[:4])
