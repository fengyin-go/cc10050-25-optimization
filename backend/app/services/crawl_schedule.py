"""采集调度计划领域服务：按周期调度价格爬取任务的执行，负责计划触发、状态回写与失败重排。

数据链路位置：调度编排层，把爬取任务按周期组织成执行计划，触发结果回写
上次状态，并把执行量同步到采集量指标。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class CrawlScheduleService(BaseModuleService):
    MODULE = "crawl_schedule"
    ENTITY = "调度计划"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["计划名称", "关联任务", "调度周期", "下次执行", "上次状态"]
    REQUIRED_FIELDS = ["计划名称", "关联任务", "调度周期"]
    STATUS_ORDER = ["待执行", "执行中", "已完成", "执行失败"]
    ACTION_RULES = {"立即执行": "执行中", "暂停计划": "待执行"}
    NEGATIVE_ACTIONS = []

    # ---- 业务扩展：周期调度与状态回写 ----

    def execute_now(self, entry_id: int) -> dict[str, Any]:
        """立即执行计划：联动关联爬取任务进入运行中。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        task_name = entry.get("关联任务", "")
        task = next(
            (t for t in store.rows("price_crawler") if t.get("任务名称") == task_name), None
        )
        if task is None:
            entry["status"] = "执行失败"
            entry["上次状态"] = "关联任务不存在"
            entry["abnormal"] = True
            return {"ok": False, "message": f"关联任务 {task_name} 不存在"}
        task["status"] = "运行中"
        task["pending"] = True
        entry["status"] = "执行中"
        entry["pending"] = True
        return {"ok": True, "message": "计划已触发执行", "task": task_name}

    def pause_plan(self, entry_id: int) -> dict[str, Any]:
        """暂停计划并同步暂停关联任务。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "计划不存在"}
        task_name = entry.get("关联任务", "")
        paused = 0
        for task in store.rows("price_crawler"):
            if task.get("任务名称") == task_name and task.get("status") == "运行中":
                task["status"] = "已暂停"
                task["pending"] = True
                paused += 1
        entry["status"] = "待执行"
        entry["pending"] = True
        return {"ok": True, "message": "计划已暂停", "paused_tasks": paused}

    def complete_round(self, entry_id: int, ok: bool) -> dict[str, Any]:
        """回写一轮执行结果并计算下次执行时间。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "计划不存在"}
        period = str(entry.get("调度周期", "60m") or "60m")
        try:
            minutes = int(period.rstrip("m")) if period.endswith("m") else int(period)
        except ValueError:
            minutes = 60
        now = datetime.now(timezone.utc)
        entry["status"] = "已完成" if ok else "执行失败"
        entry["上次状态"] = "成功" if ok else "失败"
        entry["abnormal"] = not ok
        entry["下次执行"] = datetime.fromtimestamp(
            now.timestamp() + minutes * 60, tz=timezone.utc
        ).isoformat()
        return {
            "ok": True,
            "message": "执行结果已回写",
            "next_run": entry.get("下次执行"),
        }

    def reschedule_failed(self, entry_id: int) -> dict[str, Any]:
        """失败计划重新排队并隔离失败上下文。

        隔离项必须带样本真实来源（来源站点/站点标识）与失败阶段，才能进入恢复候选单
        的同批筛选；站点信息从关联爬取任务的目标站点推导。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "执行失败":
            return {"ok": False, "message": "计划不存在或未失败"}
        task_name = entry.get("关联任务", "")
        task = next(
            (t for t in store.rows("price_crawler") if t.get("任务名称") == task_name), None
        )
        site = str(task.get("目标站点", "")) if task else ""
        rows = store.rows("failure_quarantine")
        fid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": fid,
            "样本编号": f"SCH-{entry_id}",
            "样本指纹": f"schedule-{entry_id}",
            "来源站点": site,
            "站点标识": f"SITE::{site}" if site else "",
            "失败阶段": "调度",
            "失败原因": f"计划 {entry.get('计划名称', '')} 执行失败",
            "隔离时间": datetime.now(timezone.utc).isoformat(),
            "重试次数": "1",
            "status": "待处理",
            "pending": True,
            "abnormal": False,
        })
        entry["status"] = "待执行"
        entry["abnormal"] = False
        entry["pending"] = True
        return {"ok": True, "message": "失败计划已重排", "quarantine_id": fid}

    def report_execution(self, entry_id: int) -> dict[str, Any]:
        """把计划执行量上报采集量指标。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "计划不存在"}
        task_name = entry.get("关联任务", "")
        task = next(
            (t for t in store.rows("price_crawler") if t.get("任务名称") == task_name), None
        )
        site = task.get("目标站点", "") if task else ""
        metrics = store.rows("collect_metrics")
        row = next((m for m in metrics if m.get("站点名称") == site), None)
        if row is None:
            mid = max((int(m.get("id", 0)) for m in metrics), default=0) + 1
            metrics.append({
                "id": mid,
                "统计时间": datetime.now(timezone.utc).isoformat(),
                "站点名称": site,
                "抓取量": "1",
                "解析量": "0",
                "产出量": "0",
                "status": "正常",
                "pending": True,
                "abnormal": False,
            })
        else:
            row["抓取量"] = str(int(row.get("抓取量", "0") or 0) + 1)
        return {"ok": True, "message": "计划执行量已上报", "site": site}

    def due_plans(self, now: str | None = None) -> dict[str, Any]:
        """列出到达下次执行时间的待执行计划。"""
        current = now or datetime.now(timezone.utc).isoformat()
        due = [
            p for p in store.rows(self.MODULE)
            if p.get("status") == "待执行"
            and str(p.get("下次执行", "") or "9999") <= current
        ]
        return {
            "ok": True,
            "message": "到期计划已筛选",
            "due_count": len(due),
            "plans": [p.get("计划名称", "") for p in due],
        }
