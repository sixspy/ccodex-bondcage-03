"""尾款清尾端到端：解笼页/入笼页/账户页/对账页/审计页五页账实同源。

所有页面经同一 data/app.db（测试中为临时库）的 /api/* 取数，
本测试在尾款清零前后交叉比对五个只读面，禁止任何页面私有账外口径。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db

TAIL_CAGE_CODE = "CG-P-TAIL-001"
TAIL_PARTY = "P-TAIL"
TAIL_AMOUNT = 60000.0
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


def test_five_pages_same_source_before_and_after_tail():
    c = TestClient(app)
    h = _auth(c)
    cage = next(
        x
        for x in c.get("/api/cages", headers=h).json()["items"]
        if x["cage_code"] == TAIL_CAGE_CODE
    )

    # ---- 清尾前：五页同源核对 ----
    # 入笼页（笼面）
    cage_page = c.get(f"/api/cages/{cage['id']}", headers=h).json()
    assert cage_page["cage"]["available"] == TAIL_AMOUNT
    # 解笼页（拨付册）：仅种子首笔，无尾款
    rel_page = c.get("/api/releases", headers=h).json()["items"]
    seed_rels = [r for r in rel_page if r["cage_id"] == cage["id"]]
    assert len(seed_rels) == 1 and seed_rels[0]["is_tail"] == 0
    # 账户页
    acct_before = c.get(f"/api/bonds/{TAIL_PARTY}", headers=h).json()["account"]
    assert acct_before["frozen"] == TAIL_AMOUNT
    # 对账页：冻结 == 活笼可用
    proof_before = c.get(f"/api/proof/{TAIL_PARTY}", headers=h).json()
    assert proof_before["frozen_matches_live"] is True
    recon_before = c.get(f"/api/reconcile/{TAIL_CAGE_CODE}", headers=h).json()
    assert recon_before["balanced"] is True

    # ---- 执行尾款清尾（解笼页入口、同会话）----
    r = c.post(
        "/api/releases",
        headers=h,
        json={"cage_id": cage["id"], "tail": True, "tail_mode": "甲"},
    )
    assert r.status_code == 200
    tail_voucher = r.json()
    assert tail_voucher["is_tail"] == 1
    assert tail_voucher["tail_mode"] == "甲"

    # ---- 清尾后：五页同源再核 ----
    # 入笼页：笼可用归零、状态已解笼
    cage_after = c.get(f"/api/cages/{cage['id']}", headers=h).json()["cage"]
    assert cage_after["available"] == 0
    assert cage_after["status"] == "已解笼"
    # 解笼页：出现尾款凭证，两笔合计=笼总额
    rels_after = [
        x
        for x in c.get("/api/releases", headers=h).json()["items"]
        if x["cage_id"] == cage["id"]
    ]
    assert sum(x["amount"] for x in rels_after) == CAGE_AMOUNT
    assert any(x["is_tail"] == 1 for x in rels_after)
    # 账户页：余额与冻结各扣尾款
    acct_after = c.get(f"/api/bonds/{TAIL_PARTY}", headers=h).json()["account"]
    assert acct_after["balance"] == acct_before["balance"] - TAIL_AMOUNT
    assert acct_after["frozen"] == 0
    # 对账页：笼口径平衡 + 当事人冻结/活笼对齐
    recon = c.get(f"/api/reconcile/{TAIL_CAGE_CODE}", headers=h).json()
    assert recon["balanced"] is True
    assert recon["available"] == 0
    assert recon["released"] == CAGE_AMOUNT
    assert recon["account_frozen"] == 0
    proof = c.get(f"/api/proof/{TAIL_PARTY}", headers=h).json()
    assert proof["frozen_matches_live"] is True
    assert proof["live_cage_available"] == 0
    # 审计页：尾款动作可追溯，无偏航记录
    audits = c.get("/api/audit", headers=h).json()["items"]
    actions = [a["action"] for a in audits]
    assert "cage_tail_release" in actions
    assert "cage_tail_yaw" not in actions

    # ---- 库表直查与各页数字一致（同源铁证）----
    with connect() as conn:
        db_avail = conn.execute(
            "SELECT available FROM cages WHERE id=?", (cage["id"],)
        ).fetchone()["available"]
        db_frozen = conn.execute(
            "SELECT frozen FROM bond_accounts WHERE party_key=?", (TAIL_PARTY,)
        ).fetchone()["frozen"]
        db_released = conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM releases WHERE cage_id=? AND status='已拨付'",
            (cage["id"],),
        ).fetchone()["s"]
    assert db_avail == cage_after["available"] == recon["available"]
    assert db_frozen == acct_after["frozen"] == proof["account"]["frozen"]
    assert db_released == recon["released"] == CAGE_AMOUNT


def test_rollback_leaves_zero_change_on_business_failure():
    """甲路径失败整笔回滚：构造账户冻结不足，拨付册/笼面/账户须零改。"""
    c = TestClient(app)
    h = _auth(c)
    cage = next(
        x
        for x in c.get("/api/cages", headers=h).json()["items"]
        if x["cage_code"] == TAIL_CAGE_CODE
    )
    # 人为把账户冻结改到不足以覆盖尾款，触发服务侧守卫并回滚
    with connect() as conn:
        conn.execute(
            "UPDATE bond_accounts SET frozen=? WHERE party_key=?",
            (1.0, TAIL_PARTY),
        )
        conn.commit()
    r = c.post(
        "/api/releases",
        headers=h,
        json={"cage_id": cage["id"], "tail": True},
    )
    assert r.status_code == 409
    with connect() as conn:
        cage_row = conn.execute(
            "SELECT available,status FROM cages WHERE id=?", (cage["id"],)
        ).fetchone()
        n_rel = conn.execute(
            "SELECT COUNT(*) c FROM releases WHERE cage_id=? AND is_tail=1",
            (cage["id"],),
        ).fetchone()["c"]
        frozen = conn.execute(
            "SELECT frozen FROM bond_accounts WHERE party_key=?", (TAIL_PARTY,)
        ).fetchone()["frozen"]
    assert cage_row["available"] == TAIL_AMOUNT
    assert cage_row["status"] == "部分解笼"
    assert n_rel == 0
    assert frozen == 1.0
