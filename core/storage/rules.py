from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import RuleItemLimitError
from ..rules import parse_rule_state_integer

DEFAULT_RULE_STATE = {
    "alert_active": 0,
    "qualifying_count": 0,
    "clearing_count": 0,
    "last_value": None,
    "last_baseline": None,
    "last_observed_at": None,
    "last_signal_at": None,
    "highest_notified_level": 0,
    "status": "waiting",
}
DEFAULT_HEALTH_STATE = {"fetch_failures": 0, "health_alerted": 0}


class RuleRepositoryMixin:
    def count_items(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM items").fetchone()
        return int(row[0])

    def upsert_item(self, item: dict[str, Any]) -> dict[str, Any]:
        now = self._utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO items(
                    smis_id,item_key,appid,hash_name,cn_name,enabled,created_at,updated_at
                ) VALUES(?,?,?,?,?,1,?,?)
                ON CONFLICT(smis_id) DO UPDATE SET
                    item_key=excluded.item_key, appid=excluded.appid,
                    hash_name=excluded.hash_name, cn_name=excluded.cn_name,
                    enabled=1, updated_at=excluded.updated_at
            """,
                (
                    int(item["smis_id"]),
                    str(item["item_key"]),
                    int(item["appid"]),
                    str(item["name"]),
                    str(item["name_zh"]),
                    now,
                    now,
                ),
            )
        return self.get_item(int(item["smis_id"]))

    def get_item(self, smis_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM items WHERE smis_id=?", (int(smis_id),)).fetchone()
        return dict(row) if row else None

    def list_items(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM items"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY cn_name COLLATE NOCASE, smis_id"
        with self.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [dict(row) for row in rows]

    def resolve_items(self, query: str) -> list[dict[str, Any]]:
        query = query.strip()
        if query.isdigit():
            item = self.get_item(int(query))
            return [item] if item else []
        pattern = f"%{query.lower()}%"
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM items
                WHERE lower(hash_name) LIKE ? OR lower(cn_name) LIKE ?
                ORDER BY
                    CASE WHEN lower(hash_name)=? OR lower(cn_name)=? THEN 0 ELSE 1 END,
                    cn_name COLLATE NOCASE, smis_id
            """,
                (pattern, pattern, query.lower(), query.lower()),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_rule_items(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(DISTINCT smis_id) FROM rules WHERE enabled=1 AND operational=1").fetchone()
        return int(row[0])

    def add_rule(
        self,
        smis_id: int,
        recipient_key: str,
        rule_type: str,
        threshold: float,
        *,
        max_items: int | None = None,
    ) -> dict:
        now = self._utcnow()
        rule_type = str(rule_type).strip().lower()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            has_active_rule = conn.execute(
                "SELECT 1 FROM rules WHERE smis_id=? AND enabled=1 AND operational=1 LIMIT 1",
                (int(smis_id),),
            ).fetchone()
            if not has_active_rule and max_items is not None:
                active_items = int(
                    conn.execute(
                        "SELECT COUNT(DISTINCT smis_id) FROM rules WHERE enabled=1 AND operational=1"
                    ).fetchone()[0]
                )
                if active_items >= int(max_items):
                    raise RuleItemLimitError(int(max_items))
            existing = conn.execute(
                """
                SELECT id,threshold,operational FROM rules
                WHERE smis_id=? AND recipient_key=? AND rule_type=? AND enabled=1
                ORDER BY id
            """,
                (int(smis_id), recipient_key, rule_type),
            ).fetchall()
            if existing:
                rule_id = int(existing[0]["id"])
                previous_threshold = float(existing[0]["threshold"])
                duplicate_ids = [int(row["id"]) for row in existing[1:]]
                changed = (
                    previous_threshold != float(threshold)
                    or parse_rule_state_integer(existing[0]["operational"]) != 1
                    or bool(duplicate_ids)
                )
                if changed:
                    conn.execute(
                        "UPDATE rules SET threshold=?,operational=1,updated_at=? WHERE id=?",
                        (float(threshold), now, rule_id),
                    )
                    affected_ids = [rule_id, *duplicate_ids]
                    placeholders = ",".join("?" for _ in affected_ids)
                    conn.execute(
                        f"DELETE FROM rule_states WHERE rule_id IN ({placeholders})",
                        affected_ids,
                    )
                    for affected_id in affected_ids:
                        self._cancel_pending_rule_jobs(
                            conn,
                            affected_id,
                            recipient_key,
                        )
                    if duplicate_ids:
                        duplicate_placeholders = ",".join("?" for _ in duplicate_ids)
                        conn.execute(
                            f"DELETE FROM rules WHERE id IN ({duplicate_placeholders})",
                            duplicate_ids,
                        )
                action = "updated" if changed else "unchanged"
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO rules(
                        smis_id,recipient_key,rule_type,threshold,enabled,operational,
                        created_at,updated_at
                    )
                    VALUES(?,?,?,?,1,1,?,?)
                """,
                    (int(smis_id), recipient_key, rule_type, float(threshold), now, now),
                )
                rule_id = int(cursor.lastrowid)
                previous_threshold = None
                action = "created"
        rule = self.get_rule(rule_id)
        rule["action"] = action
        rule["previous_threshold"] = previous_threshold
        return rule

    def delete_item_if_unreferenced(self, smis_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM items
                WHERE smis_id=? AND NOT EXISTS (
                    SELECT 1 FROM rules WHERE rules.smis_id=items.smis_id
                )
            """,
                (int(smis_id),),
            )
            return bool(cursor.rowcount)

    def get_rule(self, rule_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT r.*,i.item_key,i.appid,i.hash_name,i.cn_name
                FROM rules r LEFT JOIN items i USING(smis_id) WHERE r.id=?
            """,
                (int(rule_id),),
            ).fetchone()
        return dict(row) if row else None

    def list_rules(
        self,
        *,
        recipient_key: str | None = None,
        smis_id: int | None = None,
        enabled_only: bool = True,
        operational_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if recipient_key is not None:
            clauses.append("r.recipient_key=?")
            params.append(recipient_key)
        if smis_id is not None:
            clauses.append("r.smis_id=?")
            params.append(int(smis_id))
        if enabled_only or operational_only:
            clauses.append("r.enabled=1")
        if operational_only:
            clauses.append("r.operational=1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT r.*,i.item_key,i.appid,i.hash_name,i.cn_name
                FROM rules r LEFT JOIN items i USING(smis_id) {where}
                ORDER BY i.cn_name COLLATE NOCASE,r.id
            """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def update_rule(
        self,
        rule_id: int,
        recipient_key: str,
        threshold: float,
        *,
        max_items: int | None = None,
    ) -> dict | None:
        now = self._utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT smis_id,operational FROM rules WHERE id=? AND recipient_key=? AND enabled=1",
                (int(rule_id), recipient_key),
            ).fetchone()
            if current is None:
                return None
            if parse_rule_state_integer(current["operational"]) != 1 and max_items is not None:
                has_active_rule = conn.execute(
                    "SELECT 1 FROM rules WHERE smis_id=? AND enabled=1 AND operational=1 LIMIT 1",
                    (int(current["smis_id"]),),
                ).fetchone()
                active_items = int(
                    conn.execute(
                        "SELECT COUNT(DISTINCT smis_id) FROM rules WHERE enabled=1 AND operational=1"
                    ).fetchone()[0]
                )
                if not has_active_rule and active_items >= int(max_items):
                    raise RuleItemLimitError(int(max_items))
            cursor = conn.execute(
                "UPDATE rules SET threshold=?,operational=1,updated_at=? WHERE id=? AND recipient_key=? AND enabled=1",
                (float(threshold), now, int(rule_id), recipient_key),
            )
            if not cursor.rowcount:
                return None
            conn.execute("DELETE FROM rule_states WHERE rule_id=?", (int(rule_id),))
            self._cancel_pending_rule_jobs(conn, int(rule_id), recipient_key)
        return self.get_rule(rule_id)

    def set_rule_operational(
        self,
        rule_id: int,
        recipient_key: str,
        operational: bool,
    ) -> bool:
        with self.connect() as conn:
            rule = conn.execute(
                "SELECT r.smis_id,COALESCE(i.item_key,("
                "SELECT s.item_key FROM market_snapshots s "
                "WHERE s.smis_id=r.smis_id ORDER BY s.id DESC LIMIT 1"
                "),'smis:' || r.smis_id) AS item_key FROM rules r "
                "LEFT JOIN items i ON i.smis_id=r.smis_id "
                "WHERE r.id=? AND r.recipient_key=? AND r.enabled=1",
                (int(rule_id), recipient_key),
            ).fetchone()
            if rule is None:
                return False
            cursor = conn.execute(
                "UPDATE rules SET operational=?,updated_at=? WHERE id=? AND recipient_key=? AND enabled=1",
                (
                    int(bool(operational)),
                    self._utcnow(),
                    int(rule_id),
                    recipient_key,
                ),
            )
            if not operational:
                smis_id = int(rule["smis_id"])
                remaining = conn.execute(
                    "SELECT 1 FROM rules WHERE smis_id=? AND recipient_key=? AND enabled=1 AND operational=1 LIMIT 1",
                    (smis_id, recipient_key),
                ).fetchone()
                if remaining is None:
                    conn.execute(
                        "DELETE FROM rule_health WHERE smis_id=? AND recipient_key=?",
                        (smis_id, recipient_key),
                    )
                    self._cancel_pending_health_jobs(
                        conn,
                        str(rule["item_key"]),
                        recipient_key,
                    )
        return bool(cursor.rowcount)

    def delete_rule(self, rule_id: int, recipient_key: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT r.smis_id,COALESCE(i.item_key,("
                "SELECT s.item_key FROM market_snapshots s "
                "WHERE s.smis_id=r.smis_id ORDER BY s.id DESC LIMIT 1"
                "),'smis:' || r.smis_id) AS item_key FROM rules r "
                "LEFT JOIN items i ON i.smis_id=r.smis_id "
                "WHERE r.id=? AND r.recipient_key=?",
                (int(rule_id), recipient_key),
            ).fetchone()
            if not row:
                return False
            smis_id = int(row["smis_id"])
            item_key = str(row["item_key"])
            conn.execute("DELETE FROM rules WHERE id=?", (int(rule_id),))
            conn.execute("DELETE FROM rule_states WHERE rule_id=?", (int(rule_id),))
            self._cancel_pending_rule_jobs(conn, int(rule_id), recipient_key)
            session_remaining = conn.execute(
                "SELECT COUNT(*) FROM rules WHERE smis_id=? AND recipient_key=? AND enabled=1 AND operational=1",
                (smis_id, recipient_key),
            ).fetchone()[0]
            if not session_remaining:
                conn.execute(
                    "DELETE FROM rule_health WHERE smis_id=? AND recipient_key=?",
                    (smis_id, recipient_key),
                )
                self._cancel_pending_health_jobs(conn, item_key, recipient_key)
            remaining = conn.execute("SELECT COUNT(*) FROM rules WHERE smis_id=?", (smis_id,)).fetchone()[0]
            if not remaining:
                conn.execute("DELETE FROM items WHERE smis_id=?", (smis_id,))
                conn.execute("DELETE FROM rule_health WHERE smis_id=?", (smis_id,))
        return True

    def cancel_pending_rule_notifications(
        self,
        rule_id: int,
        recipient_key: str,
    ) -> int:
        with self.connect() as conn:
            return self._cancel_pending_rule_jobs(conn, rule_id, recipient_key)

    def get_rule_state(self, rule_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM rule_states WHERE rule_id=?", (int(rule_id),)).fetchone()
        return self._rule_state_from_row(rule_id, row)

    @staticmethod
    def _rule_state_from_row(
        rule_id: int,
        row: sqlite3.Row | None,
    ) -> dict[str, Any]:
        state = dict(DEFAULT_RULE_STATE)
        if row:
            state.update(dict(row))
        state["rule_id"] = int(rule_id)
        return state

    @staticmethod
    def _prepare_rule_state(
        rule_id: int,
        state: dict[str, Any],
        changes: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, int]]:
        for key, value in changes.items():
            if key not in DEFAULT_RULE_STATE:
                raise KeyError(f"未知规则状态字段: {key}")
            state[key] = value
        integer_values = {
            key: parse_rule_state_integer(state[key])
            for key in (
                "alert_active",
                "qualifying_count",
                "clearing_count",
                "highest_notified_level",
            )
        }
        if any(value is None for value in integer_values.values()) or integer_values["alert_active"] not in {0, 1}:
            raise ValueError("规则状态整数超出有效范围")
        state["rule_id"] = int(rule_id)
        return state, {key: int(value) for key, value in integer_values.items()}

    @staticmethod
    def _upsert_rule_state(
        conn: sqlite3.Connection,
        rule_id: int,
        state: dict[str, Any],
        integer_values: dict[str, int],
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO rule_states(
                rule_id,alert_active,qualifying_count,clearing_count,last_value,
                last_baseline,last_observed_at,last_signal_at,
                highest_notified_level,status,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(rule_id) DO UPDATE SET
                alert_active=excluded.alert_active,
                qualifying_count=excluded.qualifying_count,
                clearing_count=excluded.clearing_count,
                last_value=excluded.last_value,
                last_baseline=excluded.last_baseline,
                last_observed_at=excluded.last_observed_at,
                last_signal_at=excluded.last_signal_at,
                highest_notified_level=excluded.highest_notified_level,
                status=excluded.status,updated_at=excluded.updated_at
        """,
            (
                int(rule_id),
                integer_values["alert_active"],
                integer_values["qualifying_count"],
                integer_values["clearing_count"],
                state["last_value"],
                state["last_baseline"],
                state["last_observed_at"],
                state["last_signal_at"],
                integer_values["highest_notified_level"],
                state["status"],
                now,
            ),
        )

    def update_rule_state(self, rule_id: int, **changes: Any) -> dict[str, Any]:
        now = self._utcnow()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM rule_states WHERE rule_id=?",
                (int(rule_id),),
            ).fetchone()
            state = self._rule_state_from_row(rule_id, row)
            state, integer_values = self._prepare_rule_state(
                rule_id,
                state,
                changes,
            )
            self._upsert_rule_state(conn, rule_id, state, integer_values, now)
        return self.get_rule_state(rule_id)

    def apply_rule_transition(
        self,
        rule_id: int,
        recipient_key: str,
        *,
        expected_rule_updated_at: str,
        expected_state_updated_at: str | None,
        changes: dict[str, Any],
        notification: dict[str, Any] | None = None,
    ) -> bool:
        """Atomically commit a transition only if its rule and state are current."""

        now = self._utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rule = conn.execute(
                "SELECT updated_at FROM rules WHERE id=? AND recipient_key=? AND enabled=1 AND operational=1",
                (int(rule_id), recipient_key),
            ).fetchone()
            if rule is None or str(rule["updated_at"]) != str(expected_rule_updated_at):
                return False
            row = conn.execute(
                "SELECT * FROM rule_states WHERE rule_id=?",
                (int(rule_id),),
            ).fetchone()
            actual_state_updated_at = str(row["updated_at"]) if row else None
            if actual_state_updated_at != expected_state_updated_at:
                return False
            state = self._rule_state_from_row(rule_id, row)
            state, integer_values = self._prepare_rule_state(
                rule_id,
                state,
                changes,
            )
            self._upsert_rule_state(conn, rule_id, state, integer_values, now)
            if notification is not None:
                self._enqueue_notification(
                    conn,
                    signal_key=str(notification["signal_key"]),
                    recipient_key=recipient_key,
                    event_type=str(notification["event_type"]),
                    title=str(notification["title"]),
                    content=str(notification["content"]),
                    driver=str(notification["driver"]),
                    rule_id=rule_id,
                    now=now,
                )
        return True

    def reset_rule_states(
        self,
        rule_type: str,
        *,
        operational_only: bool = False,
    ) -> int:
        """Reset evaluation state and cancel pending alerts for one rule type."""
        operational_clause = " AND enabled=1 AND operational=1" if operational_only else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT id,recipient_key FROM rules WHERE rule_type=?{operational_clause}",
                (rule_type,),
            ).fetchall()
            for row in rows:
                self._cancel_pending_rule_jobs(
                    conn,
                    int(row["id"]),
                    str(row["recipient_key"]),
                )
                conn.execute("DELETE FROM rule_states WHERE rule_id=?", (int(row["id"]),))
        return len(rows)

    def get_health_state(self, smis_id: int, recipient_key: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM rule_health WHERE smis_id=? AND recipient_key=?",
                (int(smis_id), recipient_key),
            ).fetchone()
        state = dict(DEFAULT_HEALTH_STATE)
        if row:
            state.update(dict(row))
        return state

    def update_health_state(self, smis_id: int, recipient_key: str, **changes: Any) -> dict:
        state = self.get_health_state(smis_id, recipient_key)
        state.update(changes)
        failures = parse_rule_state_integer(state["fetch_failures"])
        alerted = parse_rule_state_integer(state["health_alerted"])
        if failures is None or alerted not in {0, 1}:
            raise ValueError("健康状态整数超出有效范围")
        now = self._utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO rule_health(
                    smis_id,recipient_key,fetch_failures,health_alerted,updated_at
                ) VALUES(?,?,?,?,?) ON CONFLICT(smis_id,recipient_key) DO UPDATE SET
                    fetch_failures=excluded.fetch_failures,
                    health_alerted=excluded.health_alerted,updated_at=excluded.updated_at
            """,
                (int(smis_id), recipient_key, failures, alerted, now),
            )
        return self.get_health_state(smis_id, recipient_key)
