"""失败恢复重排领域服务：隔离区候选筛选、重排申请、迟到回执与调度联动。

数据链路位置：失败治理层与调度编排层之间。恢复候选单按样本「真实来源站点/标识」
与「失败阶段」过滤，翻页排序稳定；提交时按隔离项当前身份重新校验，区分
已处理、候选失效与本次实际入选；迟到成功回执只有样本编号，调度确认前不具备
本轮可用性，且已计入原计划的成功不能被重排重复记账；最终由采集调度计划逐笔
关联实际调度队列，生成按站重排任务并回填隔离项处理方式。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.store import store

# 可恢复阶段与重试上限，保持与 failure_quarantine 服务口径一致。
RETRY_STAGES = ("采集", "解析", "计算", "匹配", "调度", "出口")
MAX_RETRY = 5
# 候选仍可重排的重试门槛（超过则候选失效）。
RETRYABLE_LIMIT = 3
RECOVERABLE_STATUS = ("待处理", "重试中")

# 候选/提交条目状态（区别于隔离记录自身 status）。
STATE_ELIGIBLE = "可重排"
STATE_PROCESSED = "已处理"
STATE_INVALID = "候选失效"
STATE_SELECTED = "本次入选"
STATE_HELD = "待核验"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_id(table: str) -> int:
    return max((int(r.get("id", 0)) for r in store.rows(table)), default=0) + 1


class RecoveryService:
    """恢复候选与重排编排，所有判断只依赖内存仓库，保证接口可回放。"""

    # ------------------------------------------------------------------
    # 候选单：按真实来源 + 失败阶段过滤，稳定排序后再分页
    # ------------------------------------------------------------------
    def list_candidates(
        self,
        *,
        site: str | None = None,
        site_id: str | None = None,
        stage: str | None = None,
        page: int = 1,
        size: int = 5,
    ) -> dict[str, Any]:
        """分页返回恢复候选单。

        过滤主键是隔离项真实来源（来源站点/站点标识）与失败阶段，不做模糊匹配，
        避免重名站点或其他阶段的记录被选入同一批。排序固定按 id 升序，保证翻页一致。
        """
        site = (site or "").strip()
        site_id = (site_id or "").strip()
        stage = (stage or "").strip()
        page = max(int(page or 1), 1)
        size = min(max(int(size or 5), 1), 100)

        rows = list(store.rows("failure_quarantine"))
        if site:
            rows = [r for r in rows if str(r.get("来源站点", "")) == site]
        if site_id:
            rows = [r for r in rows if str(r.get("站点标识", "")) == site_id]
        if stage:
            rows = [r for r in rows if str(r.get("失败阶段", "")) == stage]
        rows.sort(key=lambda r: int(r.get("id", 0)))

        total = len(rows)
        start = (page - 1) * size
        page_rows = rows[start:start + size]
        items = [self._candidate_view(r) for r in page_rows]
        return {
            "ok": True,
            "message": "恢复候选单已筛选",
            "filters": {"来源站点": site, "站点标识": site_id, "失败阶段": stage},
            "page": page,
            "size": size,
            "total": total,
            # eligible_total 是真实符合「可重排」条件的数量，与分页解耦，
            # 值班员翻页时仍能知道整批可送入调度计划的样本规模。
            "eligible_total": sum(1 for r in rows if self._evaluate(r)["state"] == STATE_ELIGIBLE),
            "items": items,
        }

    def _candidate_view(self, row: dict[str, Any]) -> dict[str, Any]:
        """给候选行附带实时资格判断与迟到回执提示，不改动隔离项本身。"""
        verdict = self._evaluate(row)
        receipt = self._open_receipt(str(row.get("样本编号", "")))
        view = {
            "id": row.get("id"),
            "样本编号": row.get("样本编号", ""),
            "样本指纹": row.get("样本指纹", ""),
            "来源站点": row.get("来源站点", ""),
            "站点标识": row.get("站点标识", ""),
            "失败阶段": row.get("失败阶段", ""),
            "失败原因": row.get("失败原因", ""),
            "隔离时间": row.get("隔离时间", ""),
            "重试次数": row.get("重试次数", "0"),
            "status": row.get("status", ""),
            "候选状态": verdict["state"],
            "不可选原因": verdict["reason"],
            "回执状态": receipt.get("回执状态", "") if receipt else "",
            "回执待确认": bool(receipt and receipt.get("回执状态") == "待确认"),
            "处理方式": row.get("处理方式", ""),
            "重排任务": row.get("重排任务", ""),
        }
        return view

    def _evaluate(self, row: dict[str, Any]) -> dict[str, str]:
        """按隔离项当前身份实时判断是否仍满足重排条件。"""
        status = str(row.get("status", ""))
        if status in ("已恢复", "已丢弃"):
            return {"state": STATE_PROCESSED,
                    "reason": f"隔离项状态为{status}（{row.get('处理方式', '已结案')}）"}
        try:
            retries = int(str(row.get("重试次数", "0") or 0))
        except ValueError:
            retries = 0
        if retries > RETRYABLE_LIMIT or retries >= MAX_RETRY:
            return {"state": STATE_INVALID, "reason": f"重试次数 {retries} 已超可恢复上限"}
        if str(row.get("失败阶段", "")) not in RETRY_STAGES:
            return {"state": STATE_INVALID, "reason": f"失败阶段 {row.get('失败阶段')} 不支持重排"}
        if status not in RECOVERABLE_STATUS:
            return {"state": STATE_INVALID, "reason": f"当前状态 {status} 不可重排"}
        return {"state": STATE_ELIGIBLE, "reason": ""}

    # ------------------------------------------------------------------
    # 重排申请：按提交的候选身份逐笔重新校验
    # ------------------------------------------------------------------
    def submit_reschedule(
        self,
        candidate_ids: list[int],
        *,
        site: str | None = None,
        stage: str | None = None,
        plan_id: int | None = None,
    ) -> dict[str, Any]:
        """提交重排请求，逐笔回传仍满足条件 / 曾可重试现已完成 / 失效的身份结论。

        只按候选 id 取隔离项当前数据重新判断，不信任上一页快照；同批必须来源与
        失败阶段一致，重名站点（站点标识不同）和其他阶段的行会被判为候选失效，
        不会送入调度队列。
        """
        site = (site or "").strip()
        stage = (stage or "").strip()
        ids = [int(i) for i in dict.fromkeys(candidate_ids or [])]
        if not ids:
            return {"ok": False, "message": "未选择任何恢复候选", "results": []}

        request_id = _next_id("reschedule_request")
        results: list[dict[str, Any]] = []
        selected: list[dict[str, Any]] = []
        held: list[dict[str, Any]] = []

        for cid in ids:
            row = store.find("failure_quarantine", cid)
            if row is None:
                results.append({"quarantine_id": cid, "样本编号": "", "state": STATE_INVALID,
                                "reason": "隔离记录不存在或已归档", "dispatched": False})
                continue
            verdict = self._evaluate(row)
            result = {
                "quarantine_id": cid,
                "样本编号": row.get("样本编号", ""),
                "来源站点": row.get("来源站点", ""),
                "站点标识": row.get("站点标识", ""),
                "失败阶段": row.get("失败阶段", ""),
                "status": row.get("status", ""),
                "state": verdict["state"],
                "reason": verdict["reason"],
                "dispatched": False,
            }
            # 同批一致性：真实来源与失败阶段必须与筛选条件完全一致。
            if verdict["state"] == STATE_ELIGIBLE:
                mismatch = self._scope_mismatch(row, site, stage)
                if mismatch:
                    result["state"] = STATE_INVALID
                    result["reason"] = mismatch
                else:
                    receipt = self._open_receipt(str(row.get("样本编号", "")))
                    if receipt is not None and receipt.get("回执状态") == "待确认":
                        # 上游成功但调度尚未确认样本可供本轮使用：保留同一申请等候核验，
                        # 不派发补采，避免与迟到样本产生两份 E9 产物。
                        result["state"] = STATE_HELD
                        result["reason"] = "迟到成功回执待调度确认，保留申请等候核验"
                        result["receipt_id"] = receipt.get("id")
                        held.append((row, receipt, result))
                    else:
                        result["state"] = STATE_SELECTED
                        result["reason"] = "实际入选本轮重排"
                        selected.append((row, result))
            results.append(result)

        # 实际入选项逐笔关联调度队列，生成按站重排任务。
        tasks = self._dispatch_tasks(request_id, selected, plan_id)
        for (_, result), task in zip(selected, tasks):
            result["dispatched"] = True
            result["task_id"] = task["task_id"]
            result["回执"] = task["回执"]

        # 待核验项登记回执关联，确认前不生成补采任务。
        for row, receipt, result in held:
            receipt["request_id"] = request_id
            row["pending"] = True
            row["处理方式"] = "重排申请已登记，等候迟到回执核验"
            result["request_id"] = request_id

        request_row = {
            "id": request_id,
            "申请编号": f"RR-{request_id:04d}",
            "提交时间": _now(),
            "筛选快照": {"来源站点": site, "失败阶段": stage, "plan_id": plan_id},
            "候选身份": [r["quarantine_id"] for r in results],
            "入选": [r["quarantine_id"] for r in results if r["state"] == STATE_SELECTED],
            "待核验": [r["quarantine_id"] for r in results if r["state"] == STATE_HELD],
            "已处理": [r["quarantine_id"] for r in results if r["state"] == STATE_PROCESSED],
            "候选失效": [r["quarantine_id"] for r in results if r["state"] == STATE_INVALID],
            "生成任务": [t["task_id"] for t in tasks],
            "status": "执行中",
        }
        store.rows("reschedule_request").append(request_row)
        return {
            "ok": True,
            "message": f"重排申请 {request_row['申请编号']} 已处理：入选 {len(selected)} 笔，"
                       f"待核验 {len(held)} 笔，失效/已处理 {len(ids) - len(selected) - len(held)} 笔",
            "request_id": request_id,
            "request_no": request_row["申请编号"],
            "results": results,
            "tasks": tasks,
        }

    def _scope_mismatch(
        self, row: dict[str, Any], site: str, stage: str
    ) -> str:
        """同批来源/阶段一致性校验，返回空串表示一致。"""
        if site and str(row.get("来源站点", "")) != site:
            return f"真实来源站点 {row.get('来源站点')} 与筛选站点 {site} 不一致，禁止同批重排"
        if stage and str(row.get("失败阶段", "")) != stage:
            return f"真实失败阶段 {row.get('失败阶段')} 与筛选阶段 {stage} 不一致，禁止同批重排"
        return ""

    def _dispatch_tasks(
        self,
        request_id: int,
        selected: list[tuple[dict[str, Any], dict[str, Any]]],
        plan_id: int | None,
    ) -> list[dict[str, Any]]:
        """把入选隔离项逐笔关联到实际调度计划，按站生成补采任务与回执。"""
        plan = self._resolve_plan(plan_id)
        tasks: list[dict[str, Any]] = []
        task_table = store.rows("reschedule_task")
        for row, _result in selected:
            task_id = f"TASK-RR{request_id:04d}-{int(row.get('id', 0)):03d}"
            task = {
                "id": _next_id("reschedule_task"),
                "task_id": task_id,
                "request_id": request_id,
                "计划id": plan.get("id") if plan else None,
                "计划名称": plan.get("计划名称", "") if plan else "未关联计划",
                "样本编号": row.get("样本编号", ""),
                "样本指纹": row.get("样本指纹", ""),
                "来源站点": row.get("来源站点", ""),
                "站点标识": row.get("站点标识", ""),
                "失败阶段": row.get("失败阶段", ""),
                "任务类型": "补采",
                "回执": "待回执",
                "status": "已入队",
                "创建时间": _now(),
            }
            task_table.append(task)
            tasks.append(task)
            # 回填隔离项：进入重试中并记录由哪个任务承接。
            row["status"] = "重试中"
            row["pending"] = True
            row["处理方式"] = f"重排补采任务 {task_id} 已入队"
            row["重排任务"] = task_id
            row["重排申请"] = request_id
            retries = int(str(row.get("重试次数", "0") or 0)) + 1
            row["重试次数"] = str(retries)
        return tasks

    def _resolve_plan(self, plan_id: int | None) -> dict[str, Any] | None:
        plans = store.rows("crawl_schedule")
        if plan_id is not None:
            plan = store.find("crawl_schedule", int(plan_id))
            if plan is not None:
                return plan
        # 未显式指定时关联第一个待执行计划，保证候选能落到真实调度队列。
        return next((p for p in plans if p.get("status") == "待执行"), None)

    # ------------------------------------------------------------------
    # 迟到成功回执：只有样本编号，调度确认后才能决定补采还是核验结案
    # ------------------------------------------------------------------
    def ingest_receipt(self, sample_no: str) -> dict[str, Any]:
        """登记一笔迟到的上游成功回执（回执只携带样本编号）。"""
        sample_no = (sample_no or "").strip()
        if not sample_no:
            return {"ok": False, "message": "缺少样本编号"}
        existing = self._receipt_by_sample(sample_no)
        if existing is not None:
            return {"ok": True, "message": "回执已存在，幂等返回", "receipt_id": existing.get("id"),
                    "回执状态": existing.get("回执状态")}
        row = self._quarantine_by_sample(sample_no)
        receipt = {
            "id": _next_id("recovery_receipt"),
            "样本编号": sample_no,
            "样本指纹": row.get("样本指纹", "") if row else "",
            "回执状态": "待确认",
            "来源站点": row.get("来源站点", "") if row else "",
            "站点标识": row.get("站点标识", "") if row else "",
            "回执时间": _now(),
            "确认时间": "",
            "counted_in_plan": False,
            "request_id": None,
        }
        store.rows("recovery_receipt").append(receipt)
        return {"ok": True, "message": "迟到成功回执已登记，等待调度确认",
                "receipt_id": receipt["id"], "回执状态": "待确认"}

    def confirm_receipt(self, sample_no: str, usable: bool) -> dict[str, Any]:
        """调度确认迟到样本是否可供本轮使用。

        - 已计入原计划（counted_in_plan=True）：幂等返回，绝不再生成重排/补采，
          防止同一成功被新重排再记一次。
        - 有待核验重排申请且样本可用：保留同一申请，回执确认首份产物，不派发补采。
        - 样本不可用：若存在待核验申请，则补发补采任务。
        最终每个样本只确认一份产物。
        """
        sample_no = (sample_no or "").strip()
        receipt = self._receipt_by_sample(sample_no)
        if receipt is None:
            return {"ok": False, "message": f"样本 {sample_no} 没有迟到回执"}
        row = self._quarantine_by_sample(sample_no)

        # 幂等闸门：成功已计入原计划，重排不得重复记账。
        if receipt.get("counted_in_plan"):
            return {
                "ok": True,
                "message": "该成功回执已计入原采集计划，重排不再重复记账",
                "sample_no": sample_no,
                "already_counted": True,
                "回执状态": receipt.get("回执状态"),
                "task_id": row.get("重排任务", "") if row else "",
            }
        if receipt.get("回执状态") == "已确认":
            return {"ok": True, "message": "回执已确认，幂等返回", "already_counted": False,
                    "sample_no": sample_no, "回执状态": "已确认"}

        receipt["回执状态"] = "已确认"
        receipt["确认时间"] = _now()
        request_id = receipt.get("request_id")

        if usable:
            # 样本可用：以迟到样本作为唯一产物，保留同一申请，不派发补采。
            receipt["counted_in_plan"] = True
            if row is not None:
                row["status"] = "已恢复"
                row["pending"] = False
                row["abnormal"] = False
                row["处理方式"] = "迟到回执确认可用，核验首份产物恢复（未补采）"
            self._mark_request_state(request_id, "已核验", sample_no)
            self._append_task(sample_no, row, request_id, "核验", "首份产物已确认",
                              status="已完成")
            self._increment_output(row)
            return {"ok": True, "message": f"样本 {sample_no} 确认可用，保留申请核验首份产物，未派发补采",
                    "sample_no": sample_no, "action": "held_for_verification", "dispatched": False}

        # 样本不可用：待核验申请转为补采，且只生成一份补采任务。
        task = self._append_task(sample_no, row, request_id, "补采", "补采已派发",
                                 status="已入队")
        if row is not None:
            row["status"] = "重试中"
            row["pending"] = True
            row["处理方式"] = f"迟到样本不可用，派发补采任务 {task['task_id']}"
            row["重排任务"] = task["task_id"]
        self._mark_request_state(request_id, "补采中", sample_no)
        return {"ok": True, "message": f"样本 {sample_no} 不可用，已派发补采任务 {task['task_id']}",
                "sample_no": sample_no, "action": "dispatched_recrawl",
                "dispatched": True, "task_id": task["task_id"]}

    def _append_task(
        self, sample_no: str, row: dict[str, Any] | None,
        request_id: int | None, task_type: str, receipt: str, *, status: str,
    ) -> dict[str, Any]:
        plan = self._resolve_plan(None)
        task = {
            "id": _next_id("reschedule_task"),
            "task_id": f"TASK-RCPT-{sample_no}",
            "request_id": request_id,
            "计划id": plan.get("id") if plan else None,
            "计划名称": plan.get("计划名称", "") if plan else "未关联计划",
            "样本编号": sample_no,
            "样本指纹": row.get("样本指纹", "") if row else "",
            "来源站点": row.get("来源站点", "") if row else "",
            "站点标识": row.get("站点标识", "") if row else "",
            "失败阶段": row.get("失败阶段", "") if row else "",
            "任务类型": task_type,
            "回执": receipt,
            "status": status,
            "创建时间": _now(),
        }
        # 同一样本只允许一份任务：已存在则复用，杜绝双产物。
        existing = next(
            (t for t in store.rows("reschedule_task") if t.get("样本编号") == sample_no), None
        )
        if existing is not None:
            return existing
        store.rows("reschedule_task").append(task)
        return task

    def _mark_request_state(
        self, request_id: int | None, state: str, sample_no: str
    ) -> None:
        if request_id is None:
            return
        req = store.find("reschedule_request", int(request_id))
        if req is not None:
            req["status"] = state
            req.setdefault("核验样本", []).append(sample_no)

    def _increment_output(self, row: dict[str, Any] | None) -> None:
        """恢复量回写采集量指标，每个隔离项至多回写一次。"""
        if row is None or row.get("产出已回写"):
            return
        site = str(row.get("来源站点", ""))
        metrics = store.rows("collect_metrics")
        target = next((m for m in metrics if m.get("站点名称") == site), None)
        if target is None:
            target = next((m for m in metrics if m.get("status") != "断采"), None)
        if target is not None:
            try:
                target["产出量"] = str(int(str(target.get("产出量", "0") or 0)) + 1)
            except ValueError:
                target["产出量"] = "1"
        row["产出已回写"] = True

    # ------------------------------------------------------------------
    # 计划页面：把恢复候选逐笔关联到实际调度队列并查看处理范围
    # ------------------------------------------------------------------
    def plan_recovery_scope(self, plan_id: int) -> dict[str, Any]:
        """采集调度计划视角：关联队列里的重排任务、回执与隔离项回填情况。"""
        plan = store.find("crawl_schedule", int(plan_id))
        if plan is None:
            return {"ok": False, "message": f"调度计划 {plan_id} 不存在"}
        tasks = [
            t for t in store.rows("reschedule_task")
            if t.get("计划id") == plan.get("id")
        ]
        linked = []
        for task in tasks:
            qrow = self._quarantine_by_sample(str(task.get("样本编号", "")))
            linked.append({
                "task_id": task.get("task_id"),
                "样本编号": task.get("样本编号"),
                "来源站点": task.get("来源站点"),
                "站点标识": task.get("站点标识"),
                "失败阶段": task.get("失败阶段"),
                "任务类型": task.get("任务类型"),
                "回执": task.get("回执"),
                "task_status": task.get("status"),
                "隔离项状态": qrow.get("status", "") if qrow else "未找到",
                "隔离项处理方式": qrow.get("处理方式", "") if qrow else "",
            })
        # 实际可恢复范围：当前仍满足重排条件、且与计划关联站点一致的隔离项。
        recoverable = [
            self._candidate_view(r)
            for r in sorted(store.rows("failure_quarantine"), key=lambda x: int(x.get("id", 0)))
            if self._evaluate(r)["state"] == STATE_ELIGIBLE
        ]
        return {
            "ok": True,
            "message": "计划恢复关联已汇总",
            "plan": {"id": plan.get("id"), "计划名称": plan.get("计划名称"),
                     "status": plan.get("status")},
            "linked_tasks": linked,
            "linked_total": len(linked),
            "recoverable_scope": recoverable,
            "recoverable_total": len(recoverable),
        }

    def change_stage(self, quarantine_id: int, new_stage: str) -> dict[str, Any]:
        """后台推进隔离项的重试阶段（模拟翻页后同条记录阶段发生变化）。"""
        row = store.find("failure_quarantine", int(quarantine_id))
        if row is None:
            return {"ok": False, "message": "隔离记录不存在"}
        new_stage = (new_stage or "").strip()
        if new_stage not in RETRY_STAGES:
            return {"ok": False, "message": f"未知失败阶段: {new_stage}"}
        old = row.get("失败阶段", "")
        row["失败阶段"] = new_stage
        return {"ok": True, "message": f"隔离项 {quarantine_id} 失败阶段 {old} -> {new_stage}",
                "quarantine_id": quarantine_id, "旧阶段": old, "新阶段": new_stage}

    # ------------------------------------------------------------------
    # 仓库辅助
    # ------------------------------------------------------------------
    def _open_receipt(self, sample_no: str) -> dict[str, Any] | None:
        return next(
            (r for r in store.rows("recovery_receipt")
             if str(r.get("样本编号", "")) == sample_no and r.get("回执状态") == "待确认"),
            None,
        )

    def _receipt_by_sample(self, sample_no: str) -> dict[str, Any] | None:
        return next(
            (r for r in store.rows("recovery_receipt")
             if str(r.get("样本编号", "")) == sample_no),
            None,
        )

    def _quarantine_by_sample(self, sample_no: str) -> dict[str, Any] | None:
        return next(
            (r for r in store.rows("failure_quarantine")
             if str(r.get("样本编号", "")) == sample_no),
            None,
        )


recovery_service = RecoveryService()
