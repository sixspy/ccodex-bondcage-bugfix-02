"""复现脚本：审计亮成功、拨付册无行、对账假出金（BUGFIX-02 埋雷等价复现）。

仅用标准库，按 release_svc.do_release / reconcile_svc._reconcile_cage 的
真实 SQL 序列在临时文件库上重放，不触碰 data/app.db 本体。
跑法：python3 -I scripts/repro_split.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import db  # noqa: E402


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="bondcage-repro-")
    db.DB_PATH = Path(tmpdir) / "repro.db"
    db.init_db()

    with db.connect() as conn:
        # 取一只种子笼，模拟核验通过
        cage = conn.execute("SELECT * FROM cages ORDER BY id LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO attestations(cage_id,outcome,evidence,attest_gen,officer,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (cage["id"], "通过", "复现核验", cage["generation"], "admin", db.now_iso()),
        )
        conn.execute("UPDATE cages SET status='核验通过' WHERE id=?", (cage["id"],))
        conn.commit()
        cage = conn.execute("SELECT * FROM cages WHERE id=?", (cage["id"],)).fetchone()

        # ===== 等价重放埋雷版 do_release 的写入序列（release_svc.py:60-87）=====
        amount = float(cage["available"])
        ref = f"REL-{cage['cage_code']}-g{cage['generation']}-REPRO"
        conn.execute(
            "UPDATE cages SET available=?, status=? WHERE id=?",
            (0.0, "已解笼", cage["id"]),
        )
        conn.execute(
            "UPDATE bond_accounts SET frozen=frozen-?, balance=balance-? WHERE party_key=?",
            (amount, amount, cage["party_key"]),
        )
        db.write_audit(
            conn, "admin", "cage_release", cage["cage_code"],
            {"release_id": None, "amount": amount, "release_ref": ref,
             "bug": "no_release_row"},
        )
        conn.commit()

    with db.connect() as conn:
        n_rel = conn.execute("SELECT COUNT(*) c FROM releases").fetchone()["c"]
        audit = conn.execute(
            "SELECT payload FROM audit_trail WHERE action='cage_release' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(audit["payload"])
        # ===== 等价重放埋雷版对账的缺口反推（reconcile_svc.py:23-24）=====
        cage = conn.execute("SELECT * FROM cages ORDER BY id LIMIT 1").fetchone()
        booked = float(conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM releases WHERE cage_id=? "
            "AND status='已拨付' AND cage_generation=?",
            (cage["id"], cage["generation"]),
        ).fetchone()["s"])
        fake_released = booked
        if booked <= 0:
            fake_released = max(0.0, float(cage["amount"]) - float(cage["available"]))

    print("【复现结果】")
    print(f"1. 审计时间线 cage_release 已亮成功：release_ref={payload['release_ref']} "
          f"release_id={payload['release_id']}")
    print(f"2. 拨付册 releases 行数：{n_rel}（按凭证号 {ref} 查无此行）")
    print(f"3. 对账缺口反推出的假出金：{fake_released}（册内真实汇总={booked}）")
    if n_rel == 0 and payload["release_id"] is None and fake_released > 0:
        print("=> 复现成功：有审计无拨付 + 对账假出金，根因为写审计与写拨付不在同一成败边界。")
        return 0
    print("=> 复现失败：现象与预期不符。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
