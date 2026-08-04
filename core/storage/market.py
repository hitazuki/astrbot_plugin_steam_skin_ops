from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..market import MAX_SOURCE_FUTURE_SKEW_SECONDS, MarketSnapshot


class MarketRepositoryMixin:
    def save_snapshots(self, snapshots: list[MarketSnapshot]) -> int:
        if not snapshots:
            return 0
        rows = []
        for s in snapshots:
            rows.append(
                (
                    s.source,
                    s.item_key,
                    s.smis_id,
                    s.appid,
                    s.name,
                    s.name_zh,
                    s.observed_at.isoformat(),
                    s.source_updated_at.isoformat(),
                    s.kind,
                    s.buff_sell_price,
                    s.buff_sell_num,
                    s.uuyp_sell_price,
                    s.uuyp_sell_num,
                    s.c5_sell_price,
                    s.c5_sell_num,
                    s.igxe_sell_price,
                    s.igxe_sell_num,
                    s.eco_sell_price,
                    s.eco_sell_num,
                    s.steam_sell_price,
                    s.steam_sell_num,
                    s.steam_transaction_quantity,
                    s.ratio(),
                )
            )
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO market_snapshots (
                    source,item_key,smis_id,appid,name,name_zh,observed_at,
                    source_updated_at,kind,buff_sell_price,buff_sell_num,
                    uuyp_sell_price,uuyp_sell_num,c5_sell_price,c5_sell_num,
                    igxe_sell_price,igxe_sell_num,eco_sell_price,eco_sell_num,
                    steam_sell_price,steam_sell_num,steam_transaction_quantity,
                    buff_to_steam_ratio
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
                rows,
            )
            return conn.total_changes - before

    def steam_history(
        self,
        item_key: str,
        days: int = 7,
        *,
        max_current_lag_seconds: int | None = None,
    ) -> list[dict[str, Any]]:
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT kind,observed_at,source_updated_at,steam_sell_price
                FROM market_snapshots
                WHERE item_key=? AND source_updated_at>=? AND steam_sell_price>0
                ORDER BY source_updated_at
            """,
                (item_key, cutoff),
            ).fetchall()
        deduped: dict[str, float] = {}
        now = datetime.now(timezone.utc)
        for row in rows:
            try:
                source = datetime.fromisoformat(str(row["source_updated_at"]))
                if source.tzinfo is None:
                    source = source.replace(tzinfo=timezone.utc)
                if source.astimezone(timezone.utc) > now + timedelta(seconds=MAX_SOURCE_FUTURE_SKEW_SECONDS):
                    continue
                if str(row["kind"]) == "current" and max_current_lag_seconds is not None:
                    observed = datetime.fromisoformat(str(row["observed_at"]))
                    if observed.tzinfo is None:
                        observed = observed.replace(tzinfo=timezone.utc)
                    lag_seconds = (observed - source).total_seconds()
                    if lag_seconds < -MAX_SOURCE_FUTURE_SKEW_SECONDS or lag_seconds > max(
                        0, int(max_current_lag_seconds)
                    ):
                        continue
            except (TypeError, ValueError):
                continue
            deduped[str(row["source_updated_at"])] = float(row["steam_sell_price"])
        return [{"source_updated_at": key, "steam_sell_price": value} for key, value in sorted(deduped.items())]

    def market_history_rows(
        self,
        item_key: str,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Return raw successful observations for canonical analysis."""
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM market_snapshots
                WHERE item_key=? AND observed_at>=?
                ORDER BY observed_at,id
            """,
                (item_key, cutoff),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune_snapshots(self, retain_days: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(retain_days))).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM market_snapshots WHERE observed_at<? OR datetime(observed_at) IS NULL",
                (cutoff,),
            )
            return int(cursor.rowcount)

    def get_metadata(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else None

    def set_metadata(self, key: str, value: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO metadata(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
                (key, value, now),
            )

    def latest_snapshot(self, item_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM market_snapshots WHERE item_key=? AND kind='current'
                  AND datetime(observed_at) IS NOT NULL
                ORDER BY julianday(observed_at) DESC,id DESC LIMIT 1
            """,
                (item_key,),
            ).fetchone()
        return dict(row) if row else None
