"""审计拨付命运说明（FATE-AUDIT-RELEASE-01）红测三条。

失败测一：审计拨付分裂——亮成功审计则拨付册必有同凭证行，否则审计零改（甲路径）。
失败测二：对账假出金——对账只准认拨付册，禁止用笼面缺口反推。
失败测三：双人同解争用——后到只留争用痕，不写业务。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


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


def _make_pass_cage(c: TestClient, h: dict, code: str = "CG-FATE-001",
                    party: str = "P-FATE", amount: float = 50000) -> dict:
    c.post(
        "/api/bonds",
        headers=h,
        json={"party_key": party, "party_name": "命运测试方",
              "initial_deposit": 200000},
    )
    cage = c.post(
        "/api/cages/freeze",
        headers=h,
        json={"party_key": party, "contract_no": "HT-FATE-1",
              "amount": amount, "beneficiary": "受益方", "cage_code": code},
    ).json()
    r = c.post(
        "/api/attestations",
        headers=h,
        json={"cage_id": cage["id"], "outcome": "通过", "evidence": "OK"},
    )
    assert r.status_code == 200
    return cage


def test_fail1_audit_release_split_must_share_fate():
    """失败测一：成功审计与拨付册行必须同生共死，凭证号可互查。"""
    c = TestClient(app)
    h = _auth(c)
    cage = _make_pass_cage(c, h)

    rel = c.post("/api/releases", headers=h, json={"cage_id": cage["id"]})
    assert rel.status_code == 200, rel.text
    body = rel.json()
    ref = body["release_ref"]

    # 拨付册必须能按凭证号查到真实行（埋雷版本：查不到 -> 红）
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM releases WHERE release_ref=?", (ref,)
        ).fetchone()
        assert row is not None, "拨付册按凭证号查无行：审计/拨付分裂"
        assert row["status"] == "已拨付"
        assert abs(float(row["amount"]) - float(cage["amount"])) < 1e-6
        release_id = row["id"]

        # 成功审计必须携带真实 release_id 且同凭证号（埋雷版本：release_id=None -> 红）
        audit = conn.execute(
            "SELECT * FROM audit_trail WHERE action='cage_release' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert audit is not None
        import json as _json

        payload = _json.loads(audit["payload"])
        assert payload.get("release_id") == release_id
        assert payload.get("release_ref") == ref
        assert "bug" not in payload, "成功审计不得残留埋雷标记"

        # 反向恒等：每条成功审计都能在拨付册找到同行，不允许有审计无拨付
        n_audit = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='cage_release'"
        ).fetchone()["c"]
        n_rel = conn.execute(
            "SELECT COUNT(*) c FROM releases WHERE status='已拨付'"
        ).fetchone()["c"]
        assert n_audit == n_rel

    # 解笼列表页数据源同样必须含该凭证
    items = c.get("/api/releases", headers=h).json()["items"]
    assert any(x["release_ref"] == ref for x in items)
    assert all(x["id"] for x in items)


def test_fail2_reconcile_must_not_invent_payout_from_gap():
    """失败测二：对账只认拨付册；册里无行时拨付列必须为 0 且如实报失衡。"""
    c = TestClient(app)
    h = _auth(c)
    cage = _make_pass_cage(c, h, code="CG-FATE-002")
    amount = float(cage["amount"])

    c.post("/api/releases", headers=h, json={"cage_id": cage["id"]})

    recon = c.get(f"/api/reconcile/{cage['cage_code']}", headers=h).json()
    with connect() as conn:
        booked = float(conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM releases "
            "WHERE cage_id=? AND status='已拨付' AND cage_generation=1",
            (cage["id"],),
        ).fetchone()["s"])

    # 对账拨付列必须等于册内汇总，禁止缺口反推
    assert abs(float(recon["released"]) - booked) < 1e-6
    assert abs(float(recon["released"]) - amount) < 1e-6

    # 人造分裂场景：笼面被扣但拨付册缺行时，对账不得假装出金
    with connect() as conn:
        conn.execute(
            "UPDATE cages SET available=0, status='已解笼' WHERE id=?",
            (cage["id"],),
        )
        conn.execute("DELETE FROM releases WHERE cage_id=?", (cage["id"],))
        conn.commit()

    recon2 = c.get(f"/api/reconcile/{cage['cage_code']}", headers=h).json()
    assert float(recon2["released"]) == 0.0, "拨付册无行却反推出金：假出金"
    assert recon2["balanced"] is False
    assert abs(float(recon2["gap"]) - amount) < 1e-6


def test_fail3_two_tellers_same_cage_lock_contention():
    """失败测三：双人同解一笼排队抢锁，后到留争用痕且业务零写。"""
    c = TestClient(app)
    h_admin = _auth(c, "admin")
    h_clerk = _auth(c, "clerk")
    cage = _make_pass_cage(c, h_admin, code="CG-FATE-003")
    lock_key = f"cage:{cage['id']}"

    # 先由 admin 持锁（模拟先到柜员正在解笼）
    got = c.post("/api/locks/acquire", headers=h_admin,
                 json={"lock_key": lock_key})
    assert got.status_code == 200

    # clerk 后到解笼：必须 409，且只留争用痕
    late = c.post("/api/releases", headers=h_clerk,
                  json={"cage_id": cage["id"]})
    assert late.status_code == 409, late.text

    with connect() as conn:
        cont = conn.execute(
            "SELECT * FROM contention_log WHERE lock_key=? ORDER BY id DESC",
            (lock_key,),
        ).fetchall()
        assert any(x["actor"] == "clerk" and x["action"] == "争用" for x in cont)
        # 业务零写：无拨付行、无成功审计、笼面未动
        assert conn.execute(
            "SELECT COUNT(*) c FROM releases WHERE cage_id=?", (cage["id"],)
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='cage_release'"
        ).fetchone()["c"] == 0
        row = conn.execute(
            "SELECT available, status FROM cages WHERE id=?", (cage["id"],)
        ).fetchone()
        assert float(row["available"]) == float(cage["amount"])
        assert row["status"] == "核验通过"

    # 先到者释放后，后到者可正常排队完成（锁可重入正常链路）
    assert c.post("/api/locks/release", headers=h_admin,
                  json={"lock_key": lock_key}).status_code == 200
    ok = c.post("/api/releases", headers=h_clerk,
                json={"cage_id": cage["id"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["id"] is not None


def test_fail3b_concurrent_release_only_one_wins():
    """失败测三补充：真并发同解一笼，恰一笔成功，另一笔被挡（争用 409 或
    排队后状态 409，均为合法串行化），册行/审计恒为一条且不超拨。"""
    c = TestClient(app)
    h = _auth(c, "admin")
    cage = _make_pass_cage(c, h, code="CG-FATE-004")

    results: list[Exception | int] = []
    barrier = threading.Barrier(2)

    def worker(user: str) -> None:
        cc = TestClient(app)
        hh = _auth(cc, user)
        barrier.wait()
        try:
            r = cc.post("/api/releases", headers=hh,
                        json={"cage_id": cage["id"]})
            results.append(r.status_code)
        except Exception as exc:  # pragma: no cover - 仅记录并发异常
            results.append(exc)

    t1 = threading.Thread(target=worker, args=("admin",))
    t2 = threading.Thread(target=worker, args=("clerk",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert sorted(results) == [200, 409], results
    with connect() as conn:
        rels = conn.execute(
            "SELECT * FROM releases WHERE cage_id=?", (cage["id"],)
        ).fetchall()
        n_rel = len(rels)
        n_audit = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='cage_release'"
        ).fetchone()["c"]
        # 恰一笔出金，金额不超笼额，审计与册行一一对应（甲路径恒等）
        assert n_rel == 1
        assert n_audit == 1
        assert abs(float(rels[0]["amount"]) - float(cage["amount"])) < 1e-6
        row = conn.execute(
            "SELECT available, status FROM cages WHERE id=?", (cage["id"],)
        ).fetchone()
        assert float(row["available"]) == 0.0
        acct = conn.execute(
            "SELECT frozen, balance FROM bond_accounts WHERE party_key='P-FATE'"
        ).fetchone()
        assert float(acct["frozen"]) == 0.0
        # 无论走哪条串行化路径，都不得留下偏航痕
        assert conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='audit_release_yaw'"
        ).fetchone()["c"] == 0


def test_guard_yaw_rolls_back_everything(monkeypatch):
    """甲路径回滚边界：真守卫判定偏航时，册行/笼面/账户/成功审计全部零改，
    且偏航痕在业务回滚后独立留痕。"""
    import release_svc
    from audit_svc import write_release_audit_guarded

    real_guard = write_release_audit_guarded

    def sabotage_guard(conn, actor, cage_code, **kw):
        # 模拟“册行在守卫回查前消失”的真实偏航：删行后交给真守卫判定
        conn.execute("DELETE FROM releases WHERE id=?", (kw["release_id"],))
        return real_guard(conn, actor, cage_code, **kw)

    # 只定点替换守卫；不可用 monkeypatch.undo()——那会同时还原 db.DB_PATH
    monkeypatch.setattr(release_svc, "write_release_audit_guarded", sabotage_guard)

    c = TestClient(app)
    h = _auth(c)
    cage = _make_pass_cage(c, h, code="CG-FATE-005")

    r = c.post("/api/releases", headers=h, json={"cage_id": cage["id"]})
    assert r.status_code == 500

    with connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM releases WHERE cage_id=?", (cage["id"],)
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='cage_release'"
        ).fetchone()["c"] == 0
        row = conn.execute(
            "SELECT available, status FROM cages WHERE id=?", (cage["id"],)
        ).fetchone()
        assert float(row["available"]) == float(cage["amount"])
        assert row["status"] == "核验通过"
        acct = conn.execute(
            "SELECT frozen, balance FROM bond_accounts WHERE party_key='P-FATE'"
        ).fetchone()
        assert float(acct["frozen"]) == float(cage["amount"])
        assert float(acct["balance"]) == 200000.0
        # 锁已释放，可重新抢锁正常完成
        assert conn.execute(
            "SELECT COUNT(*) c FROM cage_locks WHERE lock_key=?",
            (f"cage:{cage['id']}",),
        ).fetchone()["c"] == 0
        # 偏航痕独立留痕，成功审计仍为零
        assert conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='audit_release_yaw'"
        ).fetchone()["c"] == 1

    monkeypatch.setattr(release_svc, "write_release_audit_guarded",
                        write_release_audit_guarded)
    ok = c.post("/api/releases", headers=h, json={"cage_id": cage["id"]})
    assert ok.status_code == 200
    assert ok.json()["id"] is not None
