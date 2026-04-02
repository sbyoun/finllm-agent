from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import oracledb

from agent_runtime.env import require_env


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


class OracleSQLRunner:
    def __init__(self) -> None:
        self._conn: oracledb.Connection | None = None

    def _connect(self) -> oracledb.Connection:
        if self._conn is not None:
            return self._conn

        self._conn = oracledb.connect(
            user=require_env("DB_USERNAME"),
            password=require_env("DB_PASSWORD"),
            dsn=require_env("TNS_ALIAS"),
            config_dir=require_env("WALLET_PATH"),
            wallet_location=require_env("WALLET_PATH"),
            wallet_password=require_env("WALLET_PASSWORD"),
        )
        return self._conn

    def __call__(self, sql: str) -> tuple[list[str], list[dict]]:
        conn = self._connect()
        with conn.cursor() as cursor:
            cursor.execute(sql)
            description = cursor.description or []
            columns = [item[0].lower() for item in description]
            rows = [
                {
                    columns[idx]: _json_safe(value)
                    for idx, value in enumerate(row)
                }
                for row in cursor.fetchall()
            ]
        return columns, rows
