"""SQLite 持久化：场景参数与每次推演结果。"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

DB_PATH = os.environ.get("BASIN_DB_PATH", "/data/basin.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scenarios (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    params      TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id  INTEGER NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    result       TEXT NOT NULL,
    feasible     INTEGER NOT NULL,
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_scenario ON runs(scenario_id, created_at DESC);
"""


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), 777, exist_ok=True)
    with connect() as conn:
        conn.executescript(_SCHEMA)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------- scenarios ----------------

def create_scenario(name: str, params: dict) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO scenarios(name, params, created_at) VALUES (?,?,?)",
            (name, json.dumps(params, ensure_ascii=False), time.time()),
        )
        return cur.lastrowid


def update_scenario(scenario_id: int, name: str, params: dict) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE scenarios SET name=?, params=? WHERE id=?",
            (name, json.dumps(params, ensure_ascii=False), scenario_id),
        )


def delete_scenario(scenario_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM runs WHERE scenario_id=?", (scenario_id,))
        conn.execute("DELETE FROM scenarios WHERE id=?", (scenario_id,))


def _scenario_row(row: sqlite3.Row) -> dict:
    params = json.loads(row["params"])
    return {
        "id": row["id"],
        "name": row["name"],
        "params": params,
        "created_at": row["created_at"],
    }


def list_scenarios() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*,
                   (SELECT MAX(created_at) FROM runs r WHERE r.scenario_id = s.id) AS last_run_at,
                   (SELECT feasible   FROM runs r WHERE r.scenario_id = s.id
                      ORDER BY created_at DESC LIMIT 1) AS last_feasible
            FROM scenarios s ORDER BY s.created_at DESC
            """
        ).fetchall()
    out = []
    for row in rows:
        item = _scenario_row(row)
        item["last_run_at"] = row["last_run_at"]
        item["last_feasible"] = row["last_feasible"]
        out.append(item)
    return out


def get_scenario(scenario_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM scenarios WHERE id=?", (scenario_id,)
        ).fetchone()
    return _scenario_row(row) if row else None


# ---------------- runs ----------------

def add_run(scenario_id: int, result: dict) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs(scenario_id, result, feasible, created_at)"
            " VALUES (?,?,?,?)",
            (scenario_id, json.dumps(result, ensure_ascii=False),
             1 if result.get("feasible") else 0, time.time()),
        )
        return cur.lastrowid


def list_runs(scenario_id: int, limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, feasible, created_at FROM runs"
            " WHERE scenario_id=? ORDER BY created_at DESC LIMIT ?",
            (scenario_id, limit),
        ).fetchall()
    return [
        {"id": r["id"], "feasible": bool(r["feasible"]),
         "created_at": r["created_at"]}
        for r in rows
    ]


def get_run(run_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        return None
    result = json.loads(row["result"])
    result["run_id"] = row["id"]
    result["scenario_id"] = row["scenario_id"]
    result["created_at"] = row["created_at"]
    return result
