"""端到端演练：甲路径全链路 + 五页凭证同源 + 偏航拒写（临时文件库）。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import db  # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="bondcage-e2e-")) / "e2e.db"
db.init_db()

from fastapi.testclient import TestClient  # noqa: E402
from app import app  # noqa: E402

c = TestClient(app)
tok = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
h = {"Authorization": "Bearer " + tok}

c.post("/api/bonds", headers=h, json={
    "party_key": "P-E2E", "party_name": "端到端方", "initial_deposit": 300000})
cage = c.post("/api/cages/freeze", headers=h, json={
    "party_key": "P-E2E", "contract_no": "HT-E2E-1", "amount": 120000,
    "beneficiary": "端到端受益人", "cage_code": "CG-E2E-001"}).json()
c.post("/api/attestations", headers=h, json={
    "cage_id": cage["id"], "outcome": "通过", "evidence": "验收单 E2E"})

# 部分解笼两笔，验证同笼多次拨付凭证号
r1 = c.post("/api/releases", headers=h, json={"cage_id": cage["id"], "amount": 50000}).json()
r2 = c.post("/api/releases", headers=h, json={"cage_id": cage["id"], "amount": 70000}).json()
refs = [r1["release_ref"], r2["release_ref"]]
assert r1["id"] and r2["id"], "拨付册必须返回真实行 id"
assert len(set(refs)) == 2, "凭证号须唯一"

# 页1 解笼页数据源
page_release = {x["release_ref"] for x in c.get("/api/releases", headers=h).json()["items"]}
# 页2 审计页：成功审计带真实 release_id + 同凭证
audits = c.get("/api/audit?limit=50", headers=h).json()["items"]
rel_audits = [a for a in audits if a["action"] == "cage_release"]
audit_refs = {json.loads(a["payload"])["release_ref"] for a in rel_audits}
audit_ids = [json.loads(a["payload"])["release_id"] for a in rel_audits]
# 页3 对账页：拨付列 + 凭证号
recon = c.get(f"/api/reconcile/{cage['cage_code']}", headers=h).json()
# 页4 入笼页笼详情：fate 核对
detail = c.get(f"/api/cages/{cage['id']}", headers=h).json()
# 页5 保证金户：账户冻结/余额同步扣减
bond = c.get("/api/bonds/P-E2E", headers=h).json()["account"]

print("拨付页凭证:", sorted(page_release & set(refs)))
print("审计页凭证:", sorted(audit_refs), "release_ids:", audit_ids)
print("对账页 released:", recon["released"], "凭证:", recon["release_vouchers"],
      "balanced:", recon["balanced"])
print("入笼页 fate:", detail["fate"]["aligned"], detail["fate"]["release_vouchers"])
print("保证金户 frozen/balance:", bond["frozen"], bond["balance"])

assert set(refs) <= page_release
assert audit_refs == set(refs)
assert all(aid is not None for aid in audit_ids)
assert recon["released"] == 120000.0
assert recon["release_vouchers"] == refs
assert recon["balanced"] is True
assert detail["fate"]["aligned"] is True
assert set(detail["fate"]["release_vouchers"]) == set(refs)
assert bond["frozen"] == 0.0 and bond["balance"] == 180000.0

# 偏航演练：制造真实守卫会判定偏航的条件——册行在守卫回查前被删。
# 用 monkeypatch 思路包一层：先删当前事务刚插入的册行，再调真实守卫，
# 真守卫应记 audit_release_yaw 偏航痕并抛错，解笼服务整笔回滚。
import release_svc  # noqa: E402
from audit_svc import AuditReleaseYaw, write_release_audit_guarded  # noqa: E402

_orig_guard = write_release_audit_guarded


def _yaw_guard(conn, actor, cage_code, **kw):
    conn.execute("DELETE FROM releases WHERE id=?", (kw["release_id"],))
    return _orig_guard(conn, actor, cage_code, **kw)


release_svc.write_release_audit_guarded = _yaw_guard
cage2 = c.post("/api/cages/freeze", headers=h, json={
    "party_key": "P-E2E", "contract_no": "HT-E2E-2", "amount": 10000,
    "beneficiary": "端到端受益人", "cage_code": "CG-E2E-002"}).json()
c.post("/api/attestations", headers=h, json={
    "cage_id": cage2["id"], "outcome": "通过", "evidence": "x"})
yb = c.post("/api/releases", headers=h, json={"cage_id": cage2["id"]})
release_svc.write_release_audit_guarded = _orig_guard
assert yb.status_code == 500, yb.text
with db.connect() as conn:
    n_rel = conn.execute("SELECT COUNT(*) c FROM releases WHERE cage_id=?",
                         (cage2["id"],)).fetchone()["c"]
    n_ok = conn.execute(
        "SELECT COUNT(*) c FROM audit_trail WHERE action='cage_release'").fetchone()["c"]
    n_yaw = conn.execute(
        "SELECT COUNT(*) c FROM audit_trail WHERE action='audit_release_yaw'").fetchone()["c"]
    cg = conn.execute("SELECT available,status FROM cages WHERE id=?",
                      (cage2["id"],)).fetchone()
print(f"偏航后：册行={n_rel} 成功审计={n_ok} 偏航痕={n_yaw} 笼面={dict(cg)}")
assert n_rel == 0 and n_ok == 2 and n_yaw == 1
assert cg["status"] == "核验通过" and float(cg["available"]) == 10000.0

# 争用留痕：后到 409 且 contention 有痕
lk = f"cage:{cage2['id']}"
c.post("/api/locks/acquire", headers=h, json={"lock_key": lk})
tok2 = c.post("/api/login", json={"username": "clerk", "password": "123456"}).json()["token"]
late = c.post("/api/releases", headers={"Authorization": "Bearer " + tok2},
              json={"cage_id": cage2["id"]})
assert late.status_code == 409
cont = c.get("/api/contention", headers=h).json()["items"]
assert any(x["lock_key"] == lk and x["actor"] == "clerk" for x in cont)
print("争用留痕:", [x["detail"] for x in cont if x["lock_key"] == lk])

print("\n端到端演练全部通过：五页凭证同号对齐，偏航零改，争用留痕。")
