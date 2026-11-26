"""失败恢复重排路由：恢复候选单、重排申请、迟到回执确认与计划恢复关联。

这些接口承载失败隔离区与采集调度计划之间的真实业务差异，不与通用资源路由共用
动作语义；路径前缀固定在 /api/recovery，注册顺序先于 /api/{module} 动态路由。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.services.recovery import recovery_service

router = APIRouter(prefix="/api/recovery", tags=["失败恢复重排"])


class CandidateQuery(BaseModel):
    来源站点: str | None = None
    站点标识: str | None = None
    失败阶段: str | None = None
    page: int = 1
    size: int = 5


class ReschedulePayload(BaseModel):
    candidate_ids: list[int] = Field(default_factory=list)
    来源站点: str | None = None
    失败阶段: str | None = None
    plan_id: int | None = None


class ReceiptPayload(BaseModel):
    样本编号: str


class ConfirmReceiptPayload(BaseModel):
    样本编号: str
    usable: bool = True


class StagePayload(BaseModel):
    quarantine_id: int
    失败阶段: str


@router.get("/candidates")
def list_candidates(
    来源站点: str | None = Query(default=None),
    站点标识: str | None = Query(default=None),
    失败阶段: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=5, ge=1, le=100),
) -> dict[str, Any]:
    """按真实来源站点/标识与失败阶段分页查询恢复候选单。"""
    return recovery_service.list_candidates(
        site=来源站点, site_id=站点标识, stage=失败阶段, page=page, size=size)


@router.post("/reschedule")
def submit_reschedule(payload: ReschedulePayload) -> dict[str, Any]:
    """提交上一页选中的候选身份，逐笔返回入选/待核验/已处理/候选失效结论。"""
    return recovery_service.submit_reschedule(
        payload.candidate_ids,
        site=payload.来源站点,
        stage=payload.失败阶段,
        plan_id=payload.plan_id,
    )


@router.post("/receipts")
def ingest_receipt(payload: ReceiptPayload) -> dict[str, Any]:
    """登记迟到的上游成功回执（回执只有样本编号，调度尚未确认可用性）。"""
    return recovery_service.ingest_receipt(payload.样本编号)


@router.post("/receipts/confirm")
def confirm_receipt(payload: ConfirmReceiptPayload) -> dict[str, Any]:
    """调度确认迟到样本是否可供本轮使用，决定核验首份产物或派发补采。"""
    return recovery_service.confirm_receipt(payload.样本编号, payload.usable)


@router.get("/plans/{plan_id}/scope")
def plan_recovery_scope(plan_id: int) -> dict[str, Any]:
    """计划页面核对重排任务关联、回执与隔离项回填、实际可恢复范围。"""
    return recovery_service.plan_recovery_scope(plan_id)


@router.post("/stage")
def change_stage(payload: StagePayload) -> dict[str, Any]:
    """后台推进某隔离项的重试阶段，用于复现翻页后同条记录阶段变化。"""
    return recovery_service.change_stage(payload.quarantine_id, payload.失败阶段)
