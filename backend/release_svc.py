"""解笼拨付：核验通过后把笼内可用额拨付给受益人。

FATE-AUDIT-RELEASE-01 甲路径：插拨付册行、改笼面账户、守卫成功审计
处于同一事务同一成败边界，任何一步失败整笔回滚，杜绝有审计无拨付。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from audit_svc import (
    AuditReleaseYaw,
    record_release_yaw,
    write_release_audit_guarded,
)
from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso
from locks import release, require_lock
from models import ReleaseIn

router = APIRouter(tags=["release"])


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
    with connect() as conn:
        # 抢锁失败：只留争用痕，不进入业务事务（见 locks.try_acquire）
        require_lock(conn, lock_key, user)
        try:
            cage = conn.execute(
                "SELECT * FROM cages WHERE id=?", (body.cage_id,)
            ).fetchone()
            if not cage:
                raise HTTPException(404, "笼不存在")
            if cage["status"] != "核验通过" and cage["status"] != "部分解笼":
                raise HTTPException(409, "仅核验通过的笼可解笼拨付")
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
            amount = float(body.amount) if body.amount is not None else avail
            if amount <= 0:
                raise HTTPException(400, "拨付金额须大于 0")
            if amount > avail + 1e-9:
                raise HTTPException(409, "拨付金额超过笼可用额")
            payee = body.payee or cage["beneficiary"]
            now = now_iso()
            gen = int(cage["generation"])
            stamp = int(now.replace(":", "").replace("-", "").replace("T", "")[-8:])
            ref_prefix = f"REL-{cage['cage_code']}-g{gen}-{stamp}"

            # 甲路径第 1 步：先落拨付册真实行，拿到 release_id
            cur = conn.execute(
                """INSERT INTO releases(cage_id,amount,payee,cage_generation,
                   status,release_ref,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (body.cage_id, amount, payee, gen, "已拨付", ref_prefix, now),
            )
            release_id = cur.lastrowid
            # 凭证号以册行 id 收尾，保证同笼同秒多笔也唯一可查
            ref = f"{ref_prefix}-R{release_id}"
            conn.execute(
                "UPDATE releases SET release_ref=? WHERE id=?", (ref, release_id)
            )

            # 甲路径第 2 步：同事务改笼面与保证金户
            new_avail = round(avail - amount, 6)
            new_status = "已解笼" if new_avail <= 1e-9 else "部分解笼"
            conn.execute(
                "UPDATE cages SET available=?, status=? WHERE id=?",
                (max(new_avail, 0.0), new_status, body.cage_id),
            )
            conn.execute(
                """UPDATE bond_accounts
                   SET frozen = frozen - ?, balance = balance - ?
                   WHERE party_key=?""",
                (amount, amount, cage["party_key"]),
            )

            # 甲路径第 3 步：守卫审计——回查拨付册行对得上才写成功审计。
            # 偏航则抛 AuditReleaseYaw，下方整笔回滚业务（册行/笼面/账户/成功审计
            # 全部零改），回滚完成后再用独立连接留偏航痕。
            write_release_audit_guarded(
                conn,
                user,
                cage["cage_code"],
                release_id=release_id,
                release_ref=ref,
                amount=amount,
                cage_generation=gen,
                payee=payee,
            )

            conn.commit()
            row = conn.execute(
                "SELECT * FROM releases WHERE id=?", (release_id,)
            ).fetchone()
            return dict(row)
        except AuditReleaseYaw as exc:
            # 同成败边界：册行、笼面、账户、成功审计全部零改
            conn.rollback()
            # 业务写锁已释放，独立连接留偏航痕（告警不随业务回滚消失）
            record_release_yaw(exc.yaw_reason, exc.yaw_detail)
            raise
        except Exception:
            # 其他失败：整笔回滚，册行、笼面、账户、审计全部零改
            conn.rollback()
            raise
        finally:
            release(conn, lock_key, user)
            conn.commit()
