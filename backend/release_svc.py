"""解笼拨付：核验通过后把笼内可用额拨付给受益人。

尾款解笼口径见 docs/尾款解笼纪要.md（2026-11-22 当日锁死）：
- 仅走甲路径——一笔提交事务内落拨付册、扣笼可用、减账户余额与冻结、记审计；
- 禁止影子拨付单/晋升式（乙）写法，违者记「尾款偏航」并拒写；
- 两人同清一笼尾款抢同一把笼锁，后到只留争用痕、业务零写。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, require_lock
from models import ReleaseIn

router = APIRouter(tags=["release"])

# 尾款解笼纪要锁：path 只能是 甲；乙（影子晋升）禁用。
TAIL_POLICY_LOCKED = {
    "path": "甲",
    "locked_date": "2026-11-22",
    "shadow_promotion_allowed": False,
    "memo": "docs/尾款解笼纪要.md",
}
_TAILABLE_STATUS = ("核验通过", "部分解笼")


def _ref(cage_code: str, gen: int, now: str, is_tail: bool) -> str:
    stamp = now.replace(":", "").replace("-", "").replace("T", "")[-8:]
    kind = "TAIL" if is_tail else "REL"
    return f"{kind}-{cage_code}-g{gen}-{stamp}"


def _record_yaw(conn, user: str, cage, reason: str) -> None:
    """记尾款偏航审计；与调用方的回滚事务分离，单独提交。"""
    write_audit(
        conn,
        user,
        "cage_tail_yaw",
        cage["cage_code"] if cage else "unknown",
        {"reason": reason, "policy": TAIL_POLICY_LOCKED},
    )
    conn.commit()


@router.get("/api/releases")
def list_releases(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.*, c.cage_code, c.contract_no
               FROM releases r
               JOIN cages c ON c.id=r.cage_id
               ORDER BY r.id DESC LIMIT 200"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/releases")
def do_release(body: ReleaseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"cage:{body.cage_id}"
    is_tail = bool(body.tail)
    # 锁面用独立短连接：try_acquire 内部已提交持锁/争用痕。
    # 真互斥由进程内笼锁保证并覆盖整个业务临界区；业务事务回滚卷不走争用痕。
    lock_conn = connect()
    require_lock(lock_conn, lock_key, user)
    try:
        with connect() as conn:
            try:
                cage = conn.execute(
                    "SELECT * FROM cages WHERE id=?", (body.cage_id,)
                ).fetchone()
                if not cage:
                    raise HTTPException(404, "笼不存在")

                # 纪要守界：乙路径（影子拨付单再晋升）禁用，偏航拒写。
                if is_tail and body.tail_mode and body.tail_mode != TAIL_POLICY_LOCKED["path"]:
                    _record_yaw(conn, user, cage, f"请求尾款写法={body.tail_mode}，纪要锁定为甲")
                    raise HTTPException(409, "尾款偏航：影子晋升写法已被尾款解笼纪要禁用，拒写")

                if cage["status"] not in _TAILABLE_STATUS:
                    raise HTTPException(409, "仅核验通过或部分解笼的笼可解笼拨付")
                # 尾款语义只对部分解笼笼开放；核验通过笼的全额清尾走普通整笔解笼
                if is_tail and cage["status"] != "部分解笼":
                    raise HTTPException(409, "尾款拨付仅适用于部分解笼后仍留可用额的笼")
                # 核验结论必须对齐当前笼代
                att = conn.execute(
                    """SELECT * FROM attestations
                       WHERE cage_id=? AND attest_gen=? AND outcome='通过'
                       ORDER BY id DESC LIMIT 1""",
                    (body.cage_id, int(cage["generation"])),
                ).fetchone()
                if not att:
                    raise HTTPException(409, "缺少与笼代对齐的通过核验")
                avail = float(cage["available"])
                if is_tail:
                    # 尾款=清尾：必须恰为剩余可用额，夹带自定义金额一律拒写
                    if body.amount is not None and abs(float(body.amount) - avail) > 1e-9:
                        raise HTTPException(400, "尾款须结清全部剩余可用额，金额不得自定义")
                    amount = avail
                else:
                    amount = float(body.amount) if body.amount is not None else avail
                if amount <= 0:
                    raise HTTPException(400, "拨付金额须大于 0")
                if amount > avail + 1e-9:
                    raise HTTPException(409, "拨付金额超过笼可用额")
                payee = body.payee or cage["beneficiary"]
                now = now_iso()
                gen = int(cage["generation"])
                ref = _ref(cage["cage_code"], gen, now, is_tail)

                # ===== 甲路径：以下四写在同一笔提交事务，失败整笔回滚 =====
                cur = conn.execute(
                    """INSERT INTO releases(cage_id,amount,payee,cage_generation,status,
                       release_ref,is_tail,tail_mode,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        body.cage_id,
                        amount,
                        payee,
                        gen,
                        "已拨付",
                        ref,
                        1 if is_tail else 0,
                        TAIL_POLICY_LOCKED["path"] if is_tail else None,
                        now,
                    ),
                )
                new_avail = round(avail - amount, 6)
                new_status = "已解笼" if new_avail <= 1e-9 else "部分解笼"
                conn.execute(
                    "UPDATE cages SET available=?, status=? WHERE id=?",
                    (max(new_avail, 0.0), new_status, body.cage_id),
                )
                # 账户：冻结减少，余额减少（资金真实离开系统）
                acct = conn.execute(
                    "SELECT balance, frozen FROM bond_accounts WHERE party_key=?",
                    (cage["party_key"],),
                ).fetchone()
                if not acct:
                    raise HTTPException(500, "当事人保证金户缺失，事务回滚")
                if float(acct["frozen"]) + 1e-9 < amount or float(acct["balance"]) + 1e-9 < amount:
                    raise HTTPException(409, "账户余额或冻结不足，事务回滚")
                conn.execute(
                    """UPDATE bond_accounts
                       SET frozen = frozen - ?, balance = balance - ?
                       WHERE party_key=?""",
                    (amount, amount, cage["party_key"]),
                )
                write_audit(
                    conn,
                    user,
                    "cage_tail_release" if is_tail else "cage_release",
                    cage["cage_code"],
                    {
                        "release_id": cur.lastrowid,
                        "amount": amount,
                        "payee": payee,
                        "cage_generation": gen,
                        "release_ref": ref,
                        "is_tail": is_tail,
                        "tail_mode": TAIL_POLICY_LOCKED["path"] if is_tail else None,
                        "policy_locked_date": TAIL_POLICY_LOCKED["locked_date"] if is_tail else None,
                    },
                )
                conn.commit()
                # ===== 甲路径事务边界结束 =====
                row = conn.execute(
                    "SELECT * FROM releases WHERE id=?", (cur.lastrowid,)
                ).fetchone()
                return dict(row)
            except Exception:
                conn.rollback()
                raise
    finally:
        release(lock_conn, lock_key, user)
        lock_conn.commit()
        lock_conn.close()
