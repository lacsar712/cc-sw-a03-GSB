import os
from datetime import datetime, timedelta, timezone

import psycopg
from jose import JWTError, jwt
from litestar import Litestar, Request, get, post
from litestar.exceptions import HTTPException
from litestar.status_codes import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN, HTTP_409_CONFLICT
from passlib.context import CryptContext
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from pydantic import BaseModel

from domain import judge

DSN = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:54395/spectrum")
SECRET = os.environ.get("JWT_SECRET", "spectrum-dev-secret")
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
USERS = {
    "calibrator": {"role": "writer", "password_hash": pwd.hash("calib123456")},
    "inspector": {"role": "reader", "password_hash": pwd.hash("insp123456")},
}

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id serial PRIMARY KEY,
        lamp text NOT NULL,
        nominal_nm double precision NOT NULL,
        measured_nm double precision NOT NULL,
        status text NOT NULL,
        verdict text NOT NULL DEFAULT '',
        reason text NOT NULL DEFAULT '',
        created_by text NOT NULL,
        created_at timestamptz NOT NULL,
        closed_at timestamptz
    )
    """,
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS closed_at timestamptz",
    # 同灯互斥的硬约束：同一灯种最多一单在途（status='pending'）
    "CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_open_per_lamp ON jobs (lamp) WHERE status='pending'",
    """
    CREATE TABLE IF NOT EXISTS audit_logs (
        id serial PRIMARY KEY,
        lamp text NOT NULL,
        kind text NOT NULL,
        job_id integer,
        detail text NOT NULL DEFAULT '',
        actor text NOT NULL DEFAULT '',
        created_at timestamptz NOT NULL
    )
    """,
]

JOB_COLS = "id, lamp, nominal_nm, measured_nm, status, verdict, reason, created_by, created_at, closed_at"


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


def open_job_ids(conn, lamp: str) -> list:
    rows = conn.execute(
        "SELECT id FROM jobs WHERE lamp=%s AND status='pending' ORDER BY id", (lamp,)
    ).fetchall()
    return [r["id"] for r in rows]


def write_log(conn, lamp: str, kind: str, job_id, detail: str, actor: str) -> None:
    conn.execute(
        "INSERT INTO audit_logs(lamp, kind, job_id, detail, actor, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
        (lamp, kind, job_id, detail, actor, datetime.now(timezone.utc)),
    )


def reject_conflict(lamp: str, actor: str):
    """同灯仍有未结编号：记一条冲突流水（独立事务，保证落库），再 409 拒收。"""
    with connect() as conn:
        ids = open_job_ids(conn, lamp)
        nums = "、".join(f"#{i}" for i in ids) or "未知"
        write_log(conn, lamp, "conflict", None, f"拒收：该灯仍有未结编号 {nums}", actor)
        conn.commit()
    raise HTTPException(
        status_code=HTTP_409_CONFLICT,
        detail=f"灯「{lamp}」仍有未结编号：{nums}，结案后才允许同灯再开",
    )


class LoginIn(BaseModel):
    username: str
    password: str


class JobIn(BaseModel):
    lamp: str
    nominal_nm: float
    measured_nm: float


def user_from_request(request: Request) -> dict:
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="未登录")
    try:
        payload = jwt.decode(auth[7:], SECRET, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="无效令牌") from exc
    if payload.get("sub") not in USERS:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="无效令牌")
    return {"username": payload["sub"], "role": payload.get("role")}


@get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": "spectrum-wavelength-desk"}


@post("/api/login")
async def login(data: LoginIn) -> dict:
    u = USERS.get(data.username)
    if not u or not pwd.verify(data.password, u["password_hash"]):
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="账号或密码错误")
    token = jwt.encode(
        {
            "sub": data.username,
            "role": u["role"],
            "exp": datetime.now(timezone.utc) + timedelta(hours=12),
        },
        SECRET,
        algorithm="HS256",
    )
    return {"access_token": token, "role": u["role"], "username": data.username}


@get("/api/jobs")
async def list_jobs(request: Request) -> list:
    user_from_request(request)
    with connect() as conn:
        rows = conn.execute(f"SELECT {JOB_COLS} FROM jobs ORDER BY id DESC").fetchall()
        return list(rows)


@get("/api/board")
async def board(request: Request) -> dict:
    """互斥台三栏：冲突监视 / 在途同灯 / 历史已结案。登录即可看（含巡检员）。"""
    user_from_request(request)
    with connect() as conn:
        conflicts = conn.execute(
            "SELECT id, lamp, detail, actor, created_at FROM audit_logs"
            " WHERE kind='conflict' ORDER BY id DESC LIMIT 50"
        ).fetchall()
        inflight = conn.execute(
            f"SELECT {JOB_COLS} FROM jobs WHERE status='pending' ORDER BY id"
        ).fetchall()
        history = conn.execute(
            f"SELECT {JOB_COLS} FROM jobs WHERE status='done' ORDER BY id DESC LIMIT 100"
        ).fetchall()
    return {"conflicts": list(conflicts), "inflight": list(inflight), "history": list(history)}


@get("/api/logs")
async def list_logs(request: Request, lamp: str | None = None, limit: int = 200) -> list:
    """冲突与放行流水，可按灯回翻。"""
    user_from_request(request)
    lamp = (lamp or "").strip()
    limit = max(1, min(limit, 500))
    sql = "SELECT id, lamp, kind, job_id, detail, actor, created_at FROM audit_logs"
    args: list = []
    if lamp:
        sql += " WHERE lamp=%s"
        args.append(lamp)
    sql += " ORDER BY id DESC LIMIT %s"
    args.append(limit)
    with connect() as conn:
        return list(conn.execute(sql, args).fetchall())


@get("/api/jobs/{job_id:int}")
async def get_job(request: Request, job_id: int) -> dict:
    user_from_request(request)
    with connect() as conn:
        row = conn.execute(f"SELECT {JOB_COLS} FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="任务不存在")
        return dict(row)


@post("/api/jobs")
async def create_job(request: Request, data: JobIn) -> dict:
    user = user_from_request(request)
    if user["role"] != "writer":
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="仅校准员可提交")
    lamp = data.lamp.strip()
    if not lamp:
        raise HTTPException(status_code=400, detail="灯种不能为空")
    # 提交前先问该灯是否仍有未结编号
    with connect() as conn:
        has_open = bool(open_job_ids(conn, lamp))
    if has_open:
        reject_conflict(lamp, user["username"])
    try:
        with connect() as conn:
            row = conn.execute(
                """
                INSERT INTO jobs(lamp, nominal_nm, measured_nm, status, verdict, reason, created_by, created_at)
                VALUES (%s,%s,%s,'pending','','',%s,%s) RETURNING id
                """,
                (lamp, data.nominal_nm, data.measured_nm, user["username"], datetime.now(timezone.utc)),
            ).fetchone()
            conn.commit()
    except UniqueViolation:
        # 并发下同灯抢开：唯一索引兜底，同样记冲突拒收
        reject_conflict(lamp, user["username"])
    return {"id": row["id"], "status": "pending"}


@post("/api/jobs/{job_id:int}/close")
async def close_job(request: Request, job_id: int) -> dict:
    """结案：按允差出结论，在途单转为已结案，并记放行流水。"""
    user = user_from_request(request)
    if user["role"] != "writer":
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="仅校准员可结案")
    with connect() as conn:
        row = conn.execute(
            "SELECT id, lamp, nominal_nm, measured_nm, status FROM jobs WHERE id=%s FOR UPDATE",
            (job_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="任务不存在")
        if row["status"] != "pending":
            raise HTTPException(status_code=HTTP_409_CONFLICT, detail="该单已结案")
        verdict, reason = judge(row["nominal_nm"], row["measured_nm"])
        now = datetime.now(timezone.utc)
        conn.execute(
            "UPDATE jobs SET status='done', verdict=%s, reason=%s, closed_at=%s WHERE id=%s",
            (verdict, reason, now, job_id),
        )
        write_log(conn, row["lamp"], "release", job_id, f"结案放行 #{job_id}（{verdict}）", user["username"])
        conn.commit()
    return {"id": job_id, "status": "done", "verdict": verdict, "reason": reason}


def on_startup() -> None:
    with connect() as conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)
        n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        if n == 0:
            now = datetime.now(timezone.utc)
            conn.execute(
                """
                INSERT INTO jobs(lamp, nominal_nm, measured_nm, status, verdict, reason, created_by, created_at, closed_at)
                VALUES
                ('氦灯-587', 587.56, 587.50, 'done', '合格', '偏差 0.0600 nm 在允差内', 'seed', %s, %s),
                ('汞灯-546', 546.07, 546.30, 'done', '超差', '偏差 0.2300 nm 超过允差 0.08', 'seed', %s, %s)
                """,
                (now, now, now, now),
            )
        conn.commit()


app = Litestar(
    route_handlers=[health, login, list_jobs, board, list_logs, get_job, create_job, close_job],
    on_startup=[on_startup],
)
