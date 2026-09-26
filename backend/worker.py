import os
import time
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from domain import judge

DSN = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:54395/spectrum")
# 领到单后保持“在途”的秒数：在事务持锁期间暂缓结案，便于观察同灯互斥窗口
HOLD_SECONDS = float(os.environ.get("HOLD_SECONDS", "6"))


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


def claim_one(conn):
    row = conn.execute(
        """
        SELECT id, nominal_nm, measured_nm FROM jobs
        WHERE status='pending'
        ORDER BY id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    verdict, reason = judge(row["nominal_nm"], row["measured_nm"])
    # 仍在同一事务内、行仍为 pending：留出在途窗口，期间同灯再投应被拒收
    if HOLD_SECONDS > 0:
        time.sleep(HOLD_SECONDS)
    conn.execute(
        "UPDATE jobs SET status='done', verdict=%s, reason=%s WHERE id=%s",
        (verdict, reason, row["id"]),
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
        time.sleep(0.4)


if __name__ == "__main__":
    main()
