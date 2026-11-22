"""尾款解笼失败测（红→绿）。

顺序约束：本文件先在未改造的解笼服务上见红，之后才允许改服务。
- test_tail_release_deducts_cage_and_account：尾款未扣额失败测
- test_concurrent_tail_release_leaves_only_contention：双人同清尾款争用失败测
- test_tail_yaw_is_rejected：偏离尾款解笼纪要记偏航并拒写
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db

TAIL_CAGE_CODE = "CG-P-TAIL-001"
TAIL_PARTY = "P-TAIL"
TAIL_AMOUNT = 60000.0
FIRST_RELEASE = 40000.0
CAGE_AMOUNT = 100000.0


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _tail_cage(c: TestClient, h: dict) -> dict:
    rows = c.get("/api/cages", headers=h).json()["items"]
    cage = next(x for x in rows if x["cage_code"] == TAIL_CAGE_CODE)
    assert cage["status"] == "部分解笼"
    assert cage["available"] == TAIL_AMOUNT
    return cage


# --- 失败测 1：尾款必须真实扣减笼可用并出金 ----------------------------------

def test_seed_fixture_is_partial_released_with_available_left():
    """夹具本身：部分解笼后仍留可用额，且首笔拨付册/通过核验齐备。"""
    c = TestClient(app)
    h = _auth(c)
    cage = _tail_cage(c, h)
    detail = c.get(f"/api/cages/{cage['id']}", headers=h).json()
    assert len(detail["releases"]) == 1
    assert detail["releases"][0]["amount"] == FIRST_RELEASE
    assert detail["attestations"][0]["outcome"] == "通过"
    acct = c.get(f"/api/bonds/{TAIL_PARTY}", headers=h).json()["account"]
    # 账户冻结与笼面可用初始对齐；余额=入金10万−首笔出金4万
    assert acct["frozen"] == TAIL_AMOUNT
    assert acct["balance"] == 60000.0


def test_tail_release_deducts_cage_and_account():
    c = TestClient(app)
    h = _auth(c)
    cage = _tail_cage(c, h)
    acct_before = c.get(f"/api/bonds/{TAIL_PARTY}", headers=h).json()["account"]

    r = c.post(
        "/api/releases",
        headers=h,
        json={"cage_id": cage["id"], "tail": True},
    )
    assert r.status_code == 200, r.text
    voucher = r.json()
    assert voucher["status"] == "已拨付"
    assert voucher["amount"] == TAIL_AMOUNT
    assert voucher["is_tail"] == 1

    # 笼可用真实扣减至零，笼置已解笼
    after = c.get(f"/api/cages/{cage['id']}", headers=h).json()["cage"]
    assert after["available"] == 0
    assert after["status"] == "已解笼"

    # 账户余额与冻结同步真实扣减（资金出系统）
    acct_after = c.get(f"/api/bonds/{TAIL_PARTY}", headers=h).json()["account"]
    assert acct_after["frozen"] == acct_before["frozen"] - TAIL_AMOUNT
    assert acct_after["balance"] == acct_before["balance"] - TAIL_AMOUNT

    # 尾款清零后对账平衡（笼口径 + 当事人口径）
    recon = c.get(f"/api/reconcile/{TAIL_CAGE_CODE}", headers=h).json()
    assert recon["balanced"] is True
    assert recon["available"] == 0
    assert recon["released"] == CAGE_AMOUNT
    proof = c.get(f"/api/proof/{TAIL_PARTY}", headers=h).json()
    assert proof["frozen_matches_live"] is True

    # 审计落尾款动作
    audits = c.get("/api/audit", headers=h).json()["items"]
    assert any(a["action"] == "cage_tail_release" for a in audits)


def test_tail_release_requires_full_remaining_amount():
    """尾款语义即清尾：夹带金额或金额不等于剩余可用额，拒写且零改。"""
    c = TestClient(app)
    h = _auth(c)
    cage = _tail_cage(c, h)
    r = c.post(
        "/api/releases",
        headers=h,
        json={"cage_id": cage["id"], "tail": True, "amount": 12345},
    )
    assert r.status_code == 400
    # 零改
    after = c.get(f"/api/cages/{cage['id']}", headers=h).json()["cage"]
    assert after["available"] == TAIL_AMOUNT
    assert after["status"] == "部分解笼"


# --- 失败测 2：双人同清尾款，后到只留争用痕 ----------------------------------

def test_concurrent_tail_release_leaves_only_contention():
    c = TestClient(app)
    h1 = _auth(c, "admin")
    h2 = _auth(c, "clerk")
    cage = _tail_cage(c, h1)

    # admin 先持笼锁，模拟两人同时清尾款时先到者持锁
    held = c.post(
        "/api/locks/acquire",
        headers=h1,
        json={"lock_key": f"cage:{cage['id']}"},
    )
    assert held.status_code == 200

    # 后到的 clerk 清尾款：抢锁失败，409，只留争用痕
    r = c.post(
        "/api/releases",
        headers=h2,
        json={"cage_id": cage["id"], "tail": True},
    )
    assert r.status_code == 409
    contention = c.get("/api/contention", headers=h1).json()["items"]
    assert any(
        x["lock_key"] == f"cage:{cage['id']}" and x["actor"] == "clerk"
        for x in contention
    )

    # 后到者业务零写：笼面不动、无新凭证
    after = c.get(f"/api/cages/{cage['id']}", headers=h1).json()["cage"]
    assert after["available"] == TAIL_AMOUNT
    assert after["status"] == "部分解笼"
    rels = c.get("/api/releases", headers=h1).json()["items"]
    tail_rows = [x for x in rels if x["cage_id"] == cage["id"] and x.get("is_tail")]
    assert tail_rows == []

    # 先到者释放后自行清尾成功，证明锁只拦后到、不堵正路
    c.post(
        "/api/locks/release",
        headers=h1,
        json={"lock_key": f"cage:{cage['id']}"},
    )
    ok = c.post(
        "/api/releases",
        headers=h1,
        json={"cage_id": cage["id"], "tail": True},
    )
    assert ok.status_code == 200, ok.text
    final = c.get(f"/api/cages/{cage['id']}", headers=h1).json()["cage"]
    assert final["available"] == 0
    assert final["status"] == "已解笼"


def test_parallel_tail_release_only_one_wins():
    """真并发放枪：两线程同清一笼尾款，恰成一败、笼额只扣一次。"""
    c = TestClient(app)
    h1 = _auth(c, "admin")
    h2 = _auth(c, "clerk")
    cage = _tail_cage(c, h1)
    results: list[dict] = []
    barrier = threading.Barrier(2)

    def fire(headers: dict) -> None:
        client = TestClient(app)
        barrier.wait()
        rr = client.post(
            "/api/releases",
            headers=headers,
            json={"cage_id": cage["id"], "tail": True},
        )
        results.append({"status": rr.status_code})

    t1 = threading.Thread(target=fire, args=(h1,))
    t2 = threading.Thread(target=fire, args=(h2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = sorted(x["status"] for x in results)
    assert statuses == [200, 409], statuses
    with connect() as conn:
        row = conn.execute(
            "SELECT available,status FROM cages WHERE id=?", (cage["id"],)
        ).fetchone()
        n_tail = conn.execute(
            "SELECT COUNT(*) c FROM releases WHERE cage_id=? AND is_tail=1",
            (cage["id"],),
        ).fetchone()["c"]
        n_contention = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE lock_key=?",
            (f"cage:{cage['id']}",),
        ).fetchone()["c"]
    assert row["available"] == 0
    assert row["status"] == "已解笼"
    assert n_tail == 1
    assert n_contention >= 1


# --- 纪要守界：偏航拒写 ------------------------------------------------------

def test_tail_yaw_is_rejected():
    """纪要锁甲路径；请求乙（影子晋升）写法必须记尾款偏航并拒写。"""
    c = TestClient(app)
    h = _auth(c)
    cage = _tail_cage(c, h)
    r = c.post(
        "/api/releases",
        headers=h,
        json={"cage_id": cage["id"], "tail": True, "tail_mode": "乙"},
    )
    assert r.status_code == 409
    assert "尾款偏航" in r.json()["detail"]
    audits = c.get("/api/audit", headers=h).json()["items"]
    assert any(a["action"] == "cage_tail_yaw" for a in audits)
    # 偏航零业务写
    after = c.get(f"/api/cages/{cage['id']}", headers=h).json()["cage"]
    assert after["available"] == TAIL_AMOUNT
    rels = c.get("/api/releases", headers=h).json()["items"]
    assert all(not (x["cage_id"] == cage["id"] and x.get("is_tail")) for x in rels)
