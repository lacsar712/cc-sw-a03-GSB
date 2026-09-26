import os
import time
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from domain import judge

DSN = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:54395/spectrum")
INTERVAL = float(os.environ.get("WORKER_INTERVAL", "5"))


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


def claim_one(conn):
    row = conn.execute(
        """
        SELECT id, lamp, nominal_nm, measured_nm FROM jobs
        WHERE status='pending'
        ORDER BY id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    verdict, reason = judge(row["nominal_nm"], row["measured_nm"])
    now = datetime.now(timezone.utc)
    conn.execute(
        "UPDATE jobs SET status='done', verdict=%s, reason=%s, closed_at=%s WHERE id=%s",
        (verdict, reason, now, row["id"]),
    )
    conn.execute(
        "INSERT INTO audit_logs(lamp, kind, job_id, detail, actor, created_at)"
        " VALUES (%s,'release',%s,%s,'worker',%s)",
        (row["lamp"], row["id"], f"结案放行 #{row['id']}（{verdict}）", now),
    )
    conn.commit()
    return row["id"]


def main():
    while True:
        try:
            with connect() as conn:
                claim_one(conn)
        except Exception as exc:
            print("worker err", exc, flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
