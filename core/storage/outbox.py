from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any


class OutboxRepositoryMixin:
    def enqueue_notification(
        self,
        signal_key: str,
        recipient_key: str,
        event_type: str,
        title: str,
        content: str,
        *,
        driver: str,
        rule_id: int | None = None,
    ) -> bool:
        now = self._utcnow()
        with self.connect() as conn:
            return self._enqueue_notification(
                conn,
                signal_key=signal_key,
                recipient_key=recipient_key,
                event_type=event_type,
                title=title,
                content=content,
                driver=driver,
                rule_id=rule_id,
                now=now,
            )

    @staticmethod
    def _enqueue_notification(
        conn: sqlite3.Connection,
        *,
        signal_key: str,
        recipient_key: str,
        event_type: str,
        title: str,
        content: str,
        driver: str,
        rule_id: int | None,
        now: str,
    ) -> bool:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO alert_events(
                signal_key,recipient_key,event_type,title,content,rule_id,created_at
            ) VALUES(?,?,?,?,?,?,?)
        """,
            (signal_key, recipient_key, event_type, title, content, rule_id, now),
        )
        if not cursor.rowcount:
            return False
        conn.execute(
            """
            INSERT INTO delivery_jobs(event_id,driver,status,attempts,next_attempt_at)
            VALUES(?,?,'pending',0,?)
        """,
            (int(cursor.lastrowid), driver, now),
        )
        return True

    def due_notifications(self, limit: int = 100) -> list[dict[str, Any]]:
        now = self._utcnow()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT j.id,j.event_id,j.driver,j.status,j.attempts,j.last_error,
                       j.next_attempt_at,e.recipient_key,e.title,e.content,e.event_type
                FROM delivery_jobs j JOIN alert_events e ON e.id=j.event_id
                WHERE j.status='pending' AND j.next_attempt_at<=?
                ORDER BY j.id LIMIT ?
            """,
                (now, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_notification_sent(self, notification_id: int) -> None:
        now = self._utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE delivery_jobs SET status='sent', delivered_at=?, last_error=NULL
                WHERE id=?
            """,
                (now, int(notification_id)),
            )

    def mark_notification_failed(self, notification_id: int, error: str) -> None:

        with self.connect() as conn:
            row = conn.execute("SELECT attempts FROM delivery_jobs WHERE id=?", (int(notification_id),)).fetchone()
            attempts = int(row[0] if row else 0) + 1
            delay = min(60 * (2 ** min(attempts - 1, 5)), 1800)
            next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            conn.execute(
                """
                UPDATE delivery_jobs
                SET attempts=?, last_error=?, next_attempt_at=? WHERE id=?
            """,
                (attempts, error[:1000], next_attempt, int(notification_id)),
            )

    def outbox_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM delivery_jobs GROUP BY status").fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def list_events(
        self,
        recipient_key: str,
        *,
        acknowledged: bool | None = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["e.recipient_key=?"]
        params: list[Any] = [recipient_key]
        if acknowledged is not None:
            clauses.append("e.acknowledged_at IS NOT NULL" if acknowledged else "e.acknowledged_at IS NULL")
        params.append(max(1, min(int(limit), 100)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT e.*,
                       COALESCE(j.status,'stored') AS delivery_status,
                       j.driver AS delivery_driver,j.attempts,j.last_error,j.delivered_at
                FROM alert_events e
                LEFT JOIN delivery_jobs j ON j.event_id=e.id
                WHERE {" AND ".join(clauses)}
                ORDER BY e.id DESC LIMIT ?
            """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]
