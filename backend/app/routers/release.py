"""放行交接接口：质量闸放行确认、换班阈值版本、装载窗口交接与回执核销。

这些接口把「确认」和「装载」两处消费的判定收敛到同一份 ``judgement``：
确认接口返回的 ``judgement_id`` 会原样写在放行回执和装载待办上，
装载确认时再校验一次，避免两处各算一套阈值或窗口状态。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services.release_control import release_control

router = APIRouter(prefix="/api/release", tags=["放行交接"])


class VersionPayload(BaseModel):
    threshold: float | None = Field(default=None, ge=0, le=1)
    cap: int | None = Field(default=None, ge=0, le=1000)
    handover_policy: str | None = None
    shift_id: str | None = None
    effective_at: str | None = None
    updated_by: str = "值班员"
    reason: str | None = None


class WindowPayload(BaseModel):
    fact_table: str
    state: str
    by: str = "值班员"
    reason: str | None = None
    effective_at: str | None = None


class ApplicationPayload(BaseModel):
    gate_id: str
    fact_table: str
    check_item: str = "外键命中率"
    measured: float
    window_id: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    expected_rows: int = Field(default=1000, ge=0)
    handover_policy: str | None = None
    reviewer: str = "值班员"
    client_request_id: str | None = None


class ConfirmPayload(BaseModel):
    client_request_id: str | None = None
    remark: str | None = None


class LoadPayload(BaseModel):
    receipt_id: str | None = None
    actual_rows: int | None = Field(default=None, ge=0)
    client_request_id: str | None = None


@router.get("/control")
def control_snapshot() -> dict[str, Any]:
    """值班员视图：阈值版本、窗口状态、待确认申请、回执与装载待办。"""
    return release_control.control_snapshot()


@router.post("/reset", response_model=ActionResult)
def reset_demo() -> ActionResult:
    """把演示数据恢复到「上限 2、两笔待确认、窗口开启」的初始状态。"""
    release_control.reset()
    return ActionResult(ok=True, message="放行交接演示数据已重置", entry=None)


@router.get("/versions")
def list_versions() -> dict[str, Any]:
    return {
        "items": release_control.list_versions(),
        "current": release_control.control_snapshot()["current_version"],
    }


@router.post("/versions", response_model=ActionResult)
def adjust_version(payload: VersionPayload) -> ActionResult:
    """同事调整阈值/上限，返回按新版本重新判定的预览。"""
    ok, message, detail = release_control.adjust_version(
        threshold=payload.threshold,
        cap=payload.cap,
        handover_policy=payload.handover_policy,
        shift_id=payload.shift_id,
        effective_at=payload.effective_at,
        updated_by=payload.updated_by,
        reason=payload.reason or "",
    )
    return ActionResult(ok=ok, message=message, entry=detail)


@router.post("/windows", response_model=ActionResult)
def set_window(payload: WindowPayload) -> ActionResult:
    """开启/关闭装载窗口；关闭后拒绝新放行。"""
    ok, message, detail = release_control.set_window(
        fact_table=payload.fact_table,
        state=payload.state,
        by=payload.by,
        reason=payload.reason or "",
        effective_at=payload.effective_at,
    )
    return ActionResult(ok=ok, message=message, entry=detail)


@router.get("/applications")
def list_applications() -> dict[str, Any]:
    snapshot = release_control.control_snapshot()
    return {
        "items": snapshot["applications"],
        "previews": snapshot["previews"],
        "pending_count": snapshot["pending_count"],
        "current_version": snapshot["current_version"],
        "capacity": snapshot["capacity"],
    }


@router.post("/applications", response_model=ActionResult)
def submit_application(payload: ApplicationPayload) -> ActionResult:
    """质量闸受理放行申请，冻结受理阈值版本与名额语义。"""
    ok, message, entry = release_control.submit_application(
        gate_id=payload.gate_id,
        fact_table=payload.fact_table,
        check_item=payload.check_item,
        measured=payload.measured,
        window_id=payload.window_id or "",
        window_start=payload.window_start or "",
        window_end=payload.window_end or "",
        expected_rows=payload.expected_rows,
        handover_policy=payload.handover_policy,
        reviewer=payload.reviewer,
        client_request_id=payload.client_request_id or "",
    )
    return ActionResult(ok=ok, message=message, entry=entry)


@router.get("/applications/{app_id}")
def get_application(app_id: str) -> dict[str, Any]:
    """回包不明时重新打开质量闸核对实际结论（已落库的判定与回执）。"""
    detail = release_control.get_application(app_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"放行申请 {app_id} 不存在或已归档")
    return detail


@router.get("/applications/{app_id}/preview")
def preview_application(app_id: str) -> dict[str, Any]:
    judgement = release_control.preview_application(app_id)
    if judgement is None:
        raise HTTPException(status_code=404, detail=f"放行申请 {app_id} 不存在或已归档")
    return judgement


@router.post("/applications/{app_id}/confirm", response_model=ActionResult)
def confirm_release(app_id: str, payload: ConfirmPayload | None = None) -> ActionResult:
    """质量闸确认接口：返回换班语境下实际放行或拒绝的原因。"""
    body = payload or ConfirmPayload()
    ok, message, entry = release_control.confirm_release(
        app_id, client_request_id=body.client_request_id or "",
    )
    return ActionResult(ok=ok, message=message, entry=entry)


@router.get("/handover")
def handover_projection() -> dict[str, Any]:
    """值班员查询：变更何时生效、变更前已受理未完成的提交还能否交接。"""
    return release_control.project_handover()


@router.post("/reevaluate", response_model=ActionResult)
def reevaluate_pending() -> ActionResult:
    """按当前生效阈值版本重新判定全部待确认申请。"""
    previews = release_control.reevaluate_pending()
    return ActionResult(
        ok=True,
        message=f"已按现行阈值版本重新判定 {len(previews)} 笔待确认申请",
        entry={"previews": previews, "count": len(previews)},
    )


@router.post("/windows/{window_key}/load", response_model=ActionResult)
def confirm_load(window_key: str, payload: LoadPayload | None = None) -> ActionResult:
    """装载员凭有效回执确认装载；被拒/待定/重复回执显式挡住。"""
    body = payload or LoadPayload()
    ok, message, row, extra = release_control.confirm_load(
        window_key,
        receipt_id=body.receipt_id,
        actual_rows=body.actual_rows,
        client_request_id=body.client_request_id or "",
    )
    entry = dict(extra)
    entry["window"] = row
    return ActionResult(ok=ok, message=message, entry=entry)
