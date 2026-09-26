import os
from datetime import datetime, timedelta, timezone

import psycopg
from jose import JWTError, jwt
from litestar import Litestar, Request, get, post
from litestar.exceptions import HTTPException
from litestar.response import Response
from litestar.status_codes import (
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_409_CONFLICT,
)
from passlib.context import CryptContext
from psycopg.rows import dict_row
from pydantic import BaseModel

DSN = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:54395/spectrum")
SECRET = os.environ.get("JWT_SECRET", "spectrum-dev-secret")
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
USERS = {
    "calibrator": {"role": "writer", "password_hash": pwd.hash("calib123456")},
    "inspector": {"role": "reader", "password_hash": pwd.hash("insp123456")},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id serial PRIMARY KEY,
    lamp text NOT NULL,
    nominal_nm double precision NOT NULL,
    measured_nm double precision NOT NULL,
    status text NOT NULL,
    verdict text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL
);

-- 同灯排队互斥：同一灯种最多只允许一个未结（pending）编号
CREATE UNIQUE INDEX IF NOT EXISTS jobs_lamp_open_uniq
    ON jobs (lamp)
    WHERE status = 'pending';

-- 冲突与放行流水，可按灯回翻
CREATE TABLE IF NOT EXISTS mutex_events (
    id serial PRIMARY KEY,
    lamp text NOT NULL,
    kind text NOT NULL,                 -- admit 放行 / reject 冲突拒收
    job_id integer,                     -- 放行：新单编号；拒收：被退回不建单时为空
    conflict_ids integer[] NOT NULL DEFAULT '{}',  -- 拒收时仍占用该灯的未结编号
    actor text NOT NULL DEFAULT '',
    detail text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL
);
"""


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


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
        rows = conn.execute(
            "SELECT id, lamp, nominal_nm, measured_nm, status, verdict, reason, created_by FROM jobs ORDER BY id DESC"
        ).fetchall()
        return list(rows)


@get("/api/jobs/{job_id:int}")
async def get_job(request: Request, job_id: int) -> dict:
    user_from_request(request)
    with connect() as conn:
        row = conn.execute(
            "SELECT id, lamp, nominal_nm, measured_nm, status, verdict, reason, created_by FROM jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
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
    now = datetime.now(timezone.utc)
    with connect() as conn:
        # 按灯串行化“查未结 -> 建单/拒收”，避免并发下两单同时通过
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lamp,))

        open_rows = conn.execute(
            "SELECT id FROM jobs WHERE lamp = %s AND status = 'pending' ORDER BY id",
            (lamp,),
        ).fetchall()
        conflict_ids = [r["id"] for r in open_rows]

        if conflict_ids:
            # 该灯仍有未结编号：拒收并记冲突流水
            conn.execute(
                """
                INSERT INTO mutex_events(lamp, kind, job_id, conflict_ids, actor, detail, created_at)
                VALUES (%s, 'reject', NULL, %s, %s, %s, %s)
                """,
                (
                    lamp,
                    conflict_ids,
                    user["username"],
                    f"该灯仍有未结编号 {conflict_ids}，须结案后再开",
                    now,
                ),
            )
            conn.commit()
            return Response(
                {
                    "status_code": HTTP_409_CONFLICT,
                    "detail": f"该灯仍有未结编号 {conflict_ids}，须结案后再开",
                    "conflict_ids": conflict_ids,
                    "lamp": lamp,
                },
                status_code=HTTP_409_CONFLICT,
            )

        row = conn.execute(
            """
            INSERT INTO jobs(lamp, nominal_nm, measured_nm, status, verdict, reason, created_by, created_at)
            VALUES (%s,%s,%s,'pending','','',%s,%s) RETURNING id
            """,
            (lamp, data.nominal_nm, data.measured_nm, user["username"], now),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO mutex_events(lamp, kind, job_id, conflict_ids, actor, detail, created_at)
            VALUES (%s, 'admit', %s, '{}', %s, '放行：同灯无未结编号', %s)
            """,
            (lamp, row["id"], user["username"], now),
        )
        conn.commit()
        return {"id": row["id"], "status": "pending"}


@get("/api/mutex/board")
async def mutex_board(request: Request) -> dict:
    """互斥台三块：冲突监视 / 在途同灯 / 历史已结案。"""
    user_from_request(request)
    with connect() as conn:
        in_flight = conn.execute(
            """
            SELECT id, lamp, nominal_nm, measured_nm, status, created_by, created_at
            FROM jobs WHERE status = 'pending' ORDER BY lamp, id
            """
        ).fetchall()
        history = conn.execute(
            """
            SELECT id, lamp, nominal_nm, measured_nm, status, verdict, reason, created_by, created_at
            FROM jobs WHERE status = 'done' ORDER BY id DESC LIMIT 200
            """
        ).fetchall()
        # 冲突监视：近 10 分钟有未结占用或被拒收过的灯，汇总未结编号与拒收次数
        open_by_lamp: dict[str, list] = {}
        for r in conn.execute(
            "SELECT lamp, id FROM jobs WHERE status = 'pending' ORDER BY id"
        ).fetchall():
            open_by_lamp.setdefault(r["lamp"], []).append(r["id"])

        reject_rows = conn.execute(
            """
            SELECT lamp, count(*) AS n
            FROM mutex_events
            WHERE kind = 'reject' AND created_at > now() - interval '10 minutes'
            GROUP BY lamp
            """
        ).fetchall()
        reject_count = {r["lamp"]: r["n"] for r in reject_rows}

        lamps = sorted(set(open_by_lamp) | set(reject_count))
        watch = [
            {
                "lamp": lamp,
                "open_ids": open_by_lamp.get(lamp, []),
                "reject_count": reject_count.get(lamp, 0),
            }
            for lamp in lamps
        ]
        return {
            "in_flight": [dict(r) for r in in_flight],
            "history": [dict(r) for r in history],
            "watch": watch,
        }


@get("/api/mutex/events")
async def mutex_events(request: Request, lamp: str | None = None) -> list:
    """冲突与放行流水，可按灯回翻。"""
    user_from_request(request)
    with connect() as conn:
        if lamp:
            rows = conn.execute(
                """
                SELECT id, lamp, kind, job_id, conflict_ids, actor, detail, created_at
                FROM mutex_events WHERE lamp = %s ORDER BY id DESC LIMIT 200
                """,
                (lamp.strip(),),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, lamp, kind, job_id, conflict_ids, actor, detail, created_at
                FROM mutex_events ORDER BY id DESC LIMIT 200
                """
            ).fetchall()
        return [dict(r) for r in rows]


def on_startup() -> None:
    with connect() as conn:
        conn.execute(SCHEMA)
        n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        if n == 0:
            now = datetime.now(timezone.utc)
            conn.execute(
                """
                INSERT INTO jobs(lamp, nominal_nm, measured_nm, status, verdict, reason, created_by, created_at)
                VALUES
                ('氦灯-587', 587.56, 587.50, 'done', '合格', '偏差 0.0600 nm 在允差内', 'seed', %s),
                ('汞灯-546', 546.07, 546.30, 'done', '超差', '偏差 0.2300 nm 超过允差 0.08', 'seed', %s)
                """,
                (now, now),
            )
        conn.commit()


app = Litestar(
    route_handlers=[health, login, list_jobs, get_job, create_job, mutex_board, mutex_events],
    on_startup=[on_startup],
)
