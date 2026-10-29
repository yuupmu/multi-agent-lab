import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at TEXT, payload TEXT)")

    def save(self, state):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?)",
                       (state["id"], state["created_at"], json.dumps(state, ensure_ascii=False)))

    def all(self):
        with sqlite3.connect(self.path, timeout=10) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM runs ORDER BY created_at")]
