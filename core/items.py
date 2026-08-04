from __future__ import annotations


def item_from_row(row: dict) -> dict:
    return {
        "smis_id": int(row["smis_id"]),
        "item_key": str(row["item_key"]),
        "appid": int(row["appid"]),
        "name": str(row["hash_name"]),
        "name_zh": str(row["cn_name"]),
    }
