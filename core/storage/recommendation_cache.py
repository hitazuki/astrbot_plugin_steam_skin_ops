from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


class RecommendationCacheRepositoryMixin:
    def recommendation_cache_record(self, item_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM recommendation_history_cache WHERE item_key=?",
                (str(item_key),),
            ).fetchone()
        return dict(row) if row else None

    def mark_recommendation_cache_used(
        self,
        item_key: str,
        smis_id: int,
        appid: int,
        *,
        at: datetime | None = None,
    ) -> None:
        used_at = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO recommendation_history_cache(
                    item_key,smis_id,appid,last_fetched_at,last_used_at,last_failed_at
                ) VALUES(?,?,?,NULL,?,NULL)
                ON CONFLICT(item_key) DO UPDATE SET
                    smis_id=excluded.smis_id,
                    appid=excluded.appid,
                    last_used_at=excluded.last_used_at
            """,
                (str(item_key), int(smis_id), int(appid), used_at),
            )

    def mark_recommendation_cache_fetched(
        self,
        item_key: str,
        smis_id: int,
        appid: int,
        *,
        at: datetime | None = None,
    ) -> None:
        fetched_at = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO recommendation_history_cache(
                    item_key,smis_id,appid,last_fetched_at,last_used_at,last_failed_at
                ) VALUES(?,?,?,?,?,NULL)
                ON CONFLICT(item_key) DO UPDATE SET
                    smis_id=excluded.smis_id,
                    appid=excluded.appid,
                    last_fetched_at=excluded.last_fetched_at,
                    last_used_at=excluded.last_used_at,
                    last_failed_at=NULL
            """,
                (
                    str(item_key),
                    int(smis_id),
                    int(appid),
                    fetched_at,
                    fetched_at,
                ),
            )

    def mark_recommendation_cache_failed(
        self,
        item_key: str,
        smis_id: int,
        appid: int,
        *,
        at: datetime | None = None,
    ) -> None:
        failed_at = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO recommendation_history_cache(
                    item_key,smis_id,appid,last_fetched_at,last_used_at,last_failed_at
                ) VALUES(?,?,?,NULL,?,?)
                ON CONFLICT(item_key) DO UPDATE SET
                    smis_id=excluded.smis_id,
                    appid=excluded.appid,
                    last_used_at=excluded.last_used_at,
                    last_failed_at=excluded.last_failed_at
            """,
                (
                    str(item_key),
                    int(smis_id),
                    int(appid),
                    failed_at,
                    failed_at,
                ),
            )

    def prune_recommendation_cache(
        self,
        *,
        unused_days: int = 14,
        max_entries: int = 200,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Evict old/LRU recommendation metadata and recommendation-only history."""
        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = (current_time - timedelta(days=max(1, int(unused_days)))).isoformat()
        entry_limit = max(1, int(max_entries))
        removed_entries = 0
        removed_snapshots = 0
        with self.connect() as conn:
            stale = conn.execute(
                """
                SELECT item_key FROM recommendation_history_cache
                WHERE last_used_at<? ORDER BY last_used_at,item_key
            """,
                (cutoff,),
            ).fetchall()
            remove_keys = [str(row["item_key"]) for row in stale]
            remaining = conn.execute(
                "SELECT COUNT(*) FROM recommendation_history_cache WHERE last_used_at>=?",
                (cutoff,),
            ).fetchone()[0]
            overflow = max(0, int(remaining) - entry_limit)
            if overflow:
                rows = conn.execute(
                    """
                    SELECT item_key FROM recommendation_history_cache
                    WHERE last_used_at>=?
                    ORDER BY last_used_at,item_key LIMIT ?
                """,
                    (cutoff, overflow),
                ).fetchall()
                remove_keys.extend(str(row["item_key"]) for row in rows)

            for item_key in dict.fromkeys(remove_keys):
                monitored = conn.execute(
                    "SELECT 1 FROM items WHERE item_key=? LIMIT 1",
                    (item_key,),
                ).fetchone()
                if not monitored:
                    cursor = conn.execute(
                        "DELETE FROM market_snapshots WHERE item_key=?",
                        (item_key,),
                    )
                    removed_snapshots += int(cursor.rowcount)
                cursor = conn.execute(
                    "DELETE FROM recommendation_history_cache WHERE item_key=?",
                    (item_key,),
                )
                removed_entries += int(cursor.rowcount)
        return {"cache_entries": removed_entries, "snapshots": removed_snapshots}
