"""SQLite storage for learned expressions and recent group messages.

Expressions are kept per group. When an embedding model is configured each
expression also stores a unit vector, and an in-memory matrix per group is
used to merge paraphrases and to find the expressions closest to a chat.
"""

import json
import math
import random
import sqlite3
import time
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

# How many recent messages to keep per group; older ones are only needed as
# context for the next learning batch.
KEEP_MESSAGES = 600
MAX_EXAMPLES = 3
SORT_COLUMNS = {
    "learned": "last_learned DESC",
    "count": "count DESC, last_learned DESC",
    "used": "last_used DESC",
    "created": "created_at DESC",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS expressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    situation TEXT NOT NULL,
    style TEXT NOT NULL,
    examples TEXT NOT NULL DEFAULT '[]',
    count INTEGER NOT NULL DEFAULT 1,
    use_count INTEGER NOT NULL DEFAULT 0,
    featured INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_learned REAL NOT NULL,
    last_used REAL NOT NULL DEFAULT 0,
    vector BLOB,
    vector_model TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_expressions_group ON expressions(group_id);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    text TEXT NOT NULL,
    ts REAL NOT NULL,
    learned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(group_id, id);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def unit_vector(values) -> np.ndarray:
    """Return a float32 copy of an embedding scaled to length 1."""
    vector = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def text_key(situation: str, style: str) -> str:
    """Text used to compare two expressions without an embedding model."""
    return f"{situation.strip()}|{style.strip()}"


class ExpressionStore:
    """All database access for the plugin.

    Args:
        db_path: SQLite file to open or create.
    """

    def __init__(self, db_path: Path):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        # group_id -> (model, ids, matrix) of unit vectors, rebuilt lazily.
        self._index: dict[str, tuple[str, np.ndarray, np.ndarray]] = {}

    def close(self) -> None:
        self.db.close()

    # ---- recent messages ----

    def add_message(self, group_id: str, sender: str, text: str) -> None:
        self.db.execute(
            "INSERT INTO messages (group_id, sender, text, ts) VALUES (?, ?, ?, ?)",
            (group_id, sender, text, time.time()),
        )
        self.db.execute(
            "DELETE FROM messages WHERE group_id = ? AND id <= ("
            "SELECT id FROM messages WHERE group_id = ? ORDER BY id DESC "
            "LIMIT 1 OFFSET ?)",
            (group_id, group_id, KEEP_MESSAGES),
        )
        self.db.commit()

    def unlearned_count(self, group_id: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE group_id = ? AND learned = 0",
            (group_id,),
        ).fetchone()
        return row[0]

    def learning_batch(
        self, group_id: str, limit: int, context: int
    ) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        """Return (context messages, new messages), both oldest first."""
        new = self.db.execute(
            "SELECT * FROM messages WHERE group_id = ? AND learned = 0 "
            "ORDER BY id LIMIT ?",
            (group_id, limit),
        ).fetchall()
        if not new:
            return [], []
        before = self.db.execute(
            "SELECT * FROM messages WHERE group_id = ? AND id < ? "
            "ORDER BY id DESC LIMIT ?",
            (group_id, new[0]["id"], context),
        ).fetchall()
        return list(reversed(before)), new

    def mark_learned(self, group_id: str, up_to_id: int) -> None:
        self.db.execute(
            "UPDATE messages SET learned = 1 WHERE group_id = ? AND id <= ?",
            (group_id, up_to_id),
        )
        self.db.commit()

    def recent_texts(self, group_id: str, limit: int) -> list[str]:
        rows = self.db.execute(
            "SELECT text FROM messages WHERE group_id = ? ORDER BY id DESC LIMIT ?",
            (group_id, limit),
        ).fetchall()
        return [row["text"] for row in reversed(rows)]

    # ---- state ----

    def get_state(self, key: str, default: str = "") -> str:
        row = self.db.execute(
            "SELECT value FROM state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.db.commit()

    # ---- vector index ----

    def _group_index(self, group_id: str, model: str) -> tuple[np.ndarray, np.ndarray]:
        cached = self._index.get(group_id)
        if cached and cached[0] == model:
            return cached[1], cached[2]
        rows = self.db.execute(
            "SELECT id, vector FROM expressions WHERE group_id = ? "
            "AND vector_model = ? AND vector IS NOT NULL",
            (group_id, model),
        ).fetchall()
        ids = np.array([row["id"] for row in rows], dtype=np.int64)
        if rows:
            matrix = np.stack(
                [np.frombuffer(row["vector"], dtype=np.float16) for row in rows]
            ).astype(np.float32)
        else:
            matrix = np.zeros((0, 0), dtype=np.float32)
        self._index[group_id] = (model, ids, matrix)
        return ids, matrix

    def _forget_index(self, group_id: str) -> None:
        self._index.pop(group_id, None)

    def similarities(
        self, group_id: str, model: str, query: np.ndarray
    ) -> dict[int, float]:
        """Cosine similarity of every vectorized expression in a group."""
        ids, matrix = self._group_index(group_id, model)
        if not len(ids) or matrix.shape[1] != query.shape[0]:
            return {}
        scores = matrix @ query
        return dict(zip(ids.tolist(), scores.tolist(), strict=True))

    # ---- expressions ----

    def add_or_merge(
        self,
        group_id: str,
        situation: str,
        style: str,
        example: str,
        vector: np.ndarray | None,
        model: str,
        threshold: float,
    ) -> str:
        """Store a newly learned expression, merging it into a near duplicate.

        With a vector the closest expression by cosine similarity is used;
        without one the text similarity of "situation|style" decides.

        Args:
            group_id: Group the expression was learned in.
            situation: When the expression is used.
            style: What is said.
            example: The chat message it was learned from.
            vector: Unit embedding of the expression, or None.
            model: Embedding model id the vector came from.
            threshold: Similarity at or above which two expressions merge.

        Returns:
            "merged" or "added".
        """
        now = time.time()
        best_id, best_score = None, 0.0
        if vector is not None:
            for expression_id, score in self.similarities(
                group_id, model, vector
            ).items():
                if score > best_score:
                    best_id, best_score = expression_id, score
        else:
            key = text_key(situation, style)
            for row in self.db.execute(
                "SELECT id, situation, style FROM expressions WHERE group_id = ?",
                (group_id,),
            ):
                score = SequenceMatcher(
                    None, key, text_key(row["situation"], row["style"])
                ).ratio()
                if score > best_score:
                    best_id, best_score = row["id"], score
        if best_id is not None and best_score >= threshold:
            row = self.db.execute(
                "SELECT examples FROM expressions WHERE id = ?", (best_id,)
            ).fetchone()
            examples = json.loads(row["examples"])
            if example and example not in examples:
                examples = [*examples, example][-MAX_EXAMPLES:]
            self.db.execute(
                "UPDATE expressions SET count = count + 1, last_learned = ?, "
                "examples = ? WHERE id = ?",
                (now, json.dumps(examples, ensure_ascii=False), best_id),
            )
            self.db.commit()
            return "merged"
        self.db.execute(
            "INSERT INTO expressions (group_id, situation, style, examples, "
            "created_at, last_learned, vector, vector_model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                group_id,
                situation,
                style,
                json.dumps([example] if example else [], ensure_ascii=False),
                now,
                now,
                vector.astype(np.float16).tobytes() if vector is not None else None,
                model if vector is not None else "",
            ),
        )
        self.db.commit()
        self._forget_index(group_id)
        return "added"

    def enforce_cap(self, group_id: str, cap: int) -> int:
        """Delete the least useful expressions above the cap; featured ones stay.

        Returns:
            How many expressions were deleted.
        """
        total = self.count(group_id)
        if cap <= 0 or total <= cap:
            return 0
        cursor = self.db.execute(
            "DELETE FROM expressions WHERE id IN (SELECT id FROM expressions "
            "WHERE group_id = ? AND featured = 0 "
            "ORDER BY count ASC, MAX(last_learned, last_used) ASC LIMIT ?)",
            (group_id, total - cap),
        )
        self.db.commit()
        self._forget_index(group_id)
        return cursor.rowcount

    def count(self, group_id: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM expressions WHERE group_id = ?", (group_id,)
        ).fetchone()[0]

    def candidates(self, group_ids: list[str], featured_only: bool) -> list[dict]:
        if not group_ids:
            return []
        marks = ",".join("?" * len(group_ids))
        sql = (
            "SELECT id, group_id, situation, style, count, featured FROM expressions "
            f"WHERE group_id IN ({marks})"
        )
        if featured_only:
            sql += " AND featured = 1"
        return [dict(row) for row in self.db.execute(sql, group_ids)]

    def pick(
        self,
        candidates: list[dict],
        similarity: dict[int, float] | None,
        count: int,
        rng: random.Random | None = None,
    ) -> list[dict]:
        """Choose expressions for one reply.

        With similarities, the 30 closest candidates are sampled with weight
        similarity² × (1 + ln count). Without, or when no candidate is similar
        at all, every candidate is sampled with weight 1 + ln count. Featured
        expressions weigh 1.5 times more.

        Args:
            candidates: Rows from candidates().
            similarity: Expression id -> cosine similarity, or None.
            count: How many to choose.
            rng: Random source, for tests.

        Returns:
            The chosen rows.
        """
        rng = rng or random.Random()
        pool = candidates
        if similarity:
            pool = sorted(
                (row for row in candidates if row["id"] in similarity),
                key=lambda row: similarity[row["id"]],
                reverse=True,
            )[:30]
        weighted = []
        for row in pool:
            weight = 1 + math.log(max(row["count"], 1))
            if similarity:
                weight *= max(similarity.get(row["id"], 0.0), 0.0) ** 2
            if row["featured"]:
                weight *= 1.5
            if weight > 0:
                weighted.append([weight, row])
        if similarity and not weighted:
            return self.pick(candidates, None, count, rng)
        chosen = []
        while weighted and len(chosen) < count:
            total = sum(weight for weight, _ in weighted)
            point = rng.random() * total
            for index, (weight, row) in enumerate(weighted):
                point -= weight
                if point <= 0:
                    chosen.append(row)
                    weighted.pop(index)
                    break
            else:
                chosen.append(weighted.pop()[1])
        return chosen

    def mark_used(self, ids: list[int]) -> None:
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        self.db.execute(
            "UPDATE expressions SET use_count = use_count + 1, last_used = ? "
            f"WHERE id IN ({marks})",
            [time.time(), *ids],
        )
        self.db.commit()

    # ---- management page ----

    def groups(self) -> list[str]:
        rows = self.db.execute(
            "SELECT group_id FROM expressions UNION SELECT group_id FROM messages"
        ).fetchall()
        return sorted(row[0] for row in rows)

    def stats(self, group_id: str, model: str) -> dict:
        week_ago = time.time() - 7 * 86400
        row = self.db.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(featured), 0) AS featured, "
            "COALESCE(SUM(created_at >= ?), 0) AS new_7d, "
            "COALESCE(SUM(vector IS NULL OR vector_model != ?), 0) AS no_vector "
            "FROM expressions WHERE group_id = ?",
            (week_ago, model, group_id),
        ).fetchone()
        return {
            "total": row["total"],
            "featured": row["featured"],
            "new_7d": row["new_7d"],
            "no_vector": row["no_vector"] if model else 0,
            "unlearned": self.unlearned_count(group_id),
        }

    def search(
        self,
        group_id: str,
        query: str,
        sort: str,
        featured_only: bool,
        page: int,
        size: int,
    ) -> tuple[list[dict], int]:
        where = ["group_id = ?"]
        args: list = [group_id]
        if query:
            where.append("(situation LIKE ? OR style LIKE ? OR examples LIKE ?)")
            args += [f"%{query}%"] * 3
        if featured_only:
            where.append("featured = 1")
        clause = " AND ".join(where)
        total = self.db.execute(
            f"SELECT COUNT(*) FROM expressions WHERE {clause}", args
        ).fetchone()[0]
        rows = self.db.execute(
            "SELECT id, situation, style, examples, count, use_count, featured, "
            f"created_at, last_learned, last_used FROM expressions WHERE {clause} "
            f"ORDER BY {SORT_COLUMNS.get(sort, SORT_COLUMNS['learned'])} "
            "LIMIT ? OFFSET ?",
            [*args, size, (page - 1) * size],
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["examples"] = json.loads(item["examples"])
            items.append(item)
        return items, total

    def update(
        self,
        expression_id: int,
        situation: str | None = None,
        style: str | None = None,
        featured: bool | None = None,
    ) -> str | None:
        """Edit an expression. Returns its group id, or None if it is gone.

        Editing the text drops the stored vector; the caller re-embeds it.
        """
        row = self.db.execute(
            "SELECT group_id FROM expressions WHERE id = ?", (expression_id,)
        ).fetchone()
        if row is None:
            return None
        if featured is not None:
            self.db.execute(
                "UPDATE expressions SET featured = ? WHERE id = ?",
                (int(featured), expression_id),
            )
        if situation is not None and style is not None:
            self.db.execute(
                "UPDATE expressions SET situation = ?, style = ?, vector = NULL, "
                "vector_model = '' WHERE id = ?",
                (situation, style, expression_id),
            )
            self._forget_index(row["group_id"])
        self.db.commit()
        return row["group_id"]

    def set_vector(self, expression_id: int, vector: np.ndarray, model: str) -> None:
        row = self.db.execute(
            "SELECT group_id FROM expressions WHERE id = ?", (expression_id,)
        ).fetchone()
        if row is None:
            return
        self.db.execute(
            "UPDATE expressions SET vector = ?, vector_model = ? WHERE id = ?",
            (vector.astype(np.float16).tobytes(), model, expression_id),
        )
        self.db.commit()
        self._forget_index(row["group_id"])

    def without_vector(
        self, group_id: str, model: str, limit: int
    ) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT id, situation, style FROM expressions WHERE group_id = ? "
            "AND (vector IS NULL OR vector_model != ?) LIMIT ?",
            (group_id, model, limit),
        ).fetchall()

    def delete(self, ids: list[int]) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        groups = [
            row[0]
            for row in self.db.execute(
                f"SELECT DISTINCT group_id FROM expressions WHERE id IN ({marks})", ids
            )
        ]
        cursor = self.db.execute(f"DELETE FROM expressions WHERE id IN ({marks})", ids)
        self.db.commit()
        for group_id in groups:
            self._forget_index(group_id)
        return cursor.rowcount

    def clear_group(self, group_id: str) -> int:
        cursor = self.db.execute(
            "DELETE FROM expressions WHERE group_id = ?", (group_id,)
        )
        self.db.commit()
        self._forget_index(group_id)
        return cursor.rowcount
