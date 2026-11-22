"""笼锁与争用登记（公共并发面）。

并发模型（2026-11-22 尾款解笼纪要并发口径）：
- 单进程 SQLite 只有一个写者，纯 DB 逻辑锁行存在「胜者释放后败者才抢」的窗口，
  无法覆盖整个业务临界区；故以进程内按笼分键的互斥量做真互斥裁决，
  抢锁非阻塞，后到者立即落败。
- 落败者在 contention_log 独立提交一行争用痕（action='争用'），不写任何业务数据。
- cage_locks 表仍登记当前持锁者，作为解笼页/审计页可见的持锁凭证面，
  与进程内互斥同源同生命周期。
"""
from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


# 进程内按笼分键的互斥量：真裁决，覆盖整个业务临界区
_mux_guard = threading.Lock()
_key_mux: dict[str, threading.Lock] = {}
# 持锁者登记（受 _mux_guard 保护），与 cage_locks 表同源
_holders: dict[str, str] = {}


def _mutex_for(lock_key: str) -> threading.Lock:
    with _mux_guard:
        mux = _key_mux.get(lock_key)
        if mux is None:
            mux = threading.Lock()
            _key_mux[lock_key] = mux
        return mux


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    """非阻塞抢笼锁。成功持进程内互斥并登记持锁者；失败只记争用痕。"""
    mux = _mutex_for(lock_key)
    if not mux.acquire(blocking=False):
        now = now_iso()
        with _mux_guard:
            owner = _holders.get(lock_key, "未知")
        # 争用痕独立提交，与任何业务事务分离
        conn.execute(
            """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
               VALUES (?,?,?,?,?)""",
            (lock_key, holder, "争用", f"持有者={owner}", now),
        )
        conn.commit()
        return False
    with _mux_guard:
        _holders[lock_key] = holder
    conn.execute(
        "INSERT OR REPLACE INTO cage_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
        (lock_key, holder, now_iso()),
    )
    conn.commit()
    return True


def release(conn, lock_key: str, holder: str) -> None:
    """释放笼锁：删凭证面持锁行，再放进程内互斥。"""
    with _mux_guard:
        owner = _holders.get(lock_key)
        if owner != holder:
            # 非持锁者（如抢锁落败方）：无锁可放，仅确保凭证面不残留他人锁
            return
        _holders.pop(lock_key, None)
    conn.execute(
        "DELETE FROM cage_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )
    conn.commit()
    mux = _mutex_for(lock_key)
    if mux.locked():
        mux.release()


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cage_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(
            conn,
            user,
            "lock_acquire",
            body.lock_key,
            {"ok": ok},
        )
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：未能获得锁")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
        return {"ok": True}
