"""审计只读面 + 解笼成功审计守卫（FATE-AUDIT-RELEASE-01 甲路径）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect, write_audit

router = APIRouter(tags=["audit"])


class AuditReleaseYaw(HTTPException):
    """审计拨付偏航：成功审计找不到同凭证拨付册行。

    携带 reason 与 detail，解笼服务先整笔回滚业务事务，再用独立连接留偏航痕。
    """

    def __init__(self, reason: str, detail_payload: dict | None = None):
        super().__init__(500, reason)
        self.yaw_reason = reason
        self.yaw_detail = detail_payload or {}


def write_release_audit_guarded(
    conn,
    actor: str,
    cage_code: str,
    *,
    release_id: int,
    release_ref: str,
    amount: float,
    cage_generation: int,
    payee: str,
) -> None:
    """甲路径守卫：先回查拨付册真实行，对得上才准写成功审计。

    回查不到行、或凭证号/金额/笼代对不上，记 audit_release_yaw 偏航痕并抛错，
    由解笼服务回滚整个事务——成功审计零增、拨付零增、笼面零改。
    本函数只在解笼事务内调用，不自行 commit。
    """
    row = conn.execute(
        """SELECT id, amount, release_ref, cage_generation, status
           FROM releases WHERE id=?""",
        (release_id,),
    ).fetchone()
    yaw_reason = None
    if row is None:
        yaw_reason = "拨付册无对应行，拒绝写成功审计"
    elif row["release_ref"] != release_ref:
        yaw_reason = "凭证号与拨付册不一致"
    elif abs(float(row["amount"]) - float(amount)) > 1e-6:
        yaw_reason = "金额与拨付册不一致"
    elif int(row["cage_generation"]) != int(cage_generation):
        yaw_reason = "笼代与拨付册不一致"
    elif row["status"] != "已拨付":
        yaw_reason = "拨付册行状态非已拨付"

    if yaw_reason is not None:
        # 不在业务事务内写痕（会被业务回滚一并撤掉，且 SQLite 单写者使独立连接
        # 在事务未结束时无法写入）。只抛偏航异常，由解笼服务在业务回滚完成后
        # 调 record_release_yaw() 用新连接留痕。成功审计始终零增。
        raise AuditReleaseYaw(
            yaw_reason,
            {
                "actor": actor,
                "cage_code": cage_code,
                "release_id": release_id,
                "release_ref": release_ref,
                "amount": amount,
                "cage_generation": cage_generation,
            },
        )

    # 守卫通过：才准写成功审计（与拨付册行同事务同生共死）
    write_audit(
        conn,
        actor,
        "cage_release",
        cage_code,
        {
            "release_id": release_id,
            "amount": float(amount),
            "payee": payee,
            "cage_generation": int(cage_generation),
            "release_ref": release_ref,
        },
    )


def record_release_yaw(reason: str, detail: dict) -> None:
    """业务事务回滚后调用：独立连接落偏航痕，与业务成败边界隔离。"""
    with connect() as yaw_conn:
        write_audit(
            yaw_conn,
            detail["actor"],
            "audit_release_yaw",
            detail["cage_code"],
            {
                "reason": reason,
                "release_id": detail["release_id"],
                "release_ref": detail["release_ref"],
                "amount": detail["amount"],
                "cage_generation": detail["cage_generation"],
            },
        )
        yaw_conn.commit()


@router.get("/api/audit")
def list_audit(authorization: AuthHeader = None, limit: int = 100):
    require_user(authorization)
    limit = max(1, min(int(limit), 500))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_trail ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/audit/actions")
def list_actions(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT action, COUNT(*) c FROM audit_trail
               GROUP BY action ORDER BY c DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
