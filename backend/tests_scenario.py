"""端到端业务场景验证（内存仓库，ASGI 直调，不走网络编码）。

覆盖：
1. 站点B+采集阶段筛选与稳定分页，E9 位于第二页；
2. 重名站点（SITE-B-2）与其他阶段行不进入同批；
3. 迟到成功回执待确认时，E9 提交重排被保留待核验、不派发补采；
4. 调度确认可用后只确认一份 E9 产物（核验，不补采），E11 不被替选；
5. 已计入原计划的回执幂等，不重复记账；
6. 提交身份复校：曾经可重试、现已完成的条目被判已处理；
7. 阶段在翻页后变化，从计划页面核对应有来源与实际可恢复范围。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.store import store

client = TestClient(app)
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------- 1. 筛选与分页
r = client.get("/api/recovery/candidates", params={
    "来源站点": "站点B", "失败阶段": "采集", "page": 1, "size": 5})
p1 = r.json()
check("候选单接口 200", r.status_code == 200)
check("第1页返回5条", len(p1["items"]) == 5, str([i["样本编号"] for i in p1["items"]]))
check("第1页均为站点B采集阶段", all(
    i["来源站点"] == "站点B" and i["失败阶段"] == "采集" for i in p1["items"]))
check("重名站点 E12(SITE-B-2) 不在站点B结果",
      all(i["样本编号"] != "E12" for i in p1["items"]))

r = client.get("/api/recovery/candidates", params={
    "来源站点": "站点B", "失败阶段": "采集", "page": 2, "size": 5})
p2 = r.json()
ids_p2 = [i["样本编号"] for i in p2["items"]]
check("第二页含 E9", "E9" in ids_p2, str(ids_p2))
check("第二页不含其他阶段行(E6解析/E10调度/E13匹配/E15出口)",
      not {"E6", "E10", "E13", "E15"} & set(ids_p2))
check("第二页不含站点C的 E11", "E11" not in ids_p2)
check("分页 total 一致", p1["total"] == p2["total"] and p1["total"] >= 7)
e9_view = next(i for i in p2["items"] if i["样本编号"] == "E9")
check("E9 当前可重排", e9_view["候选状态"] == "可重排", e9_view["候选状态"])
check("E9 带迟到待确认回执提示", e9_view["回执待确认"] is True, e9_view["回执状态"])

# 站点标识精确过滤：重名站点 B（SITE-B-2）单独可查，绝不串批
r = client.get("/api/recovery/candidates", params={
    "来源站点": "站点B", "站点标识": "SITE-B-2", "失败阶段": "采集"})
dup = r.json()
check("重名站点按站点标识隔离", [i["样本编号"] for i in dup["items"]] == ["E12"])

# 通用资源列表的精确过滤与稳定分页同样生效
r = client.get("/api/failure_quarantine", params={
    "来源站点": "站点B", "失败阶段": "采集", "page": 1, "size": 5})
check("隔离区列表精确过滤", r.status_code == 200 and len(r.json()["items"]) == 5)

# ---------------------------------------------------------------- 2. E11 无关性
r = client.get("/api/recovery/candidates", params={
    "来源站点": "站点C", "失败阶段": "采集"})
site_c = r.json()
check("站点C 只查得到 E11", [i["样本编号"] for i in site_c["items"]] == ["E11"])

# 模拟后台迟到回执（仅样本编号，调度未确认）——种子里 E9 已有待确认回执，
# 再登记一次验证幂等
r = client.post("/api/recovery/receipts", json={"样本编号": "E9"})
check("重复回执幂等登记", r.json()["ok"] is True and r.json()["回执状态"] == "待确认")

# ---------------------------------------------------------------- 3. 提交 E9 重排
e9_id = next(q["id"] for q in store.rows("failure_quarantine") if q["样本编号"] == "E9")
r = client.post("/api/recovery/reschedule", json={
    "candidate_ids": [e9_id], "来源站点": "站点B", "失败阶段": "采集"})
sub = r.json()
check("重排提交成功", sub["ok"], sub.get("message", ""))
res = sub["results"][0]
check("E9 因待确认回执进入待核验（未补采）",
      res["state"] == "待核验" and res["dispatched"] is False, res["state"])
check("待核验未生成任何重排任务", sub["tasks"] == [])
e9_row = store.find("failure_quarantine", e9_id)
check("E9 隔离项保留且未进重试中", e9_row["status"] == "待处理", e9_row["status"])

# ---------------------------------------------------------------- 4. 同批混入 E11 与 E6(其他阶段) 必须被拒
e11_id = next(q["id"] for q in store.rows("failure_quarantine") if q["样本编号"] == "E11")
e6_id = next(q["id"] for q in store.rows("failure_quarantine") if q["样本编号"] == "E6")
r = client.post("/api/recovery/reschedule", json={
    "candidate_ids": [e11_id, e6_id], "来源站点": "站点B", "失败阶段": "采集"})
sub2 = r.json()
by_sample = {x["样本编号"]: x for x in sub2["results"]}
check("E11 跨站点被判候选失效", by_sample["E11"]["state"] == "候选失效", by_sample["E11"]["reason"])
check("E6 跨阶段被判候选失效", by_sample["E6"]["state"] == "候选失效", by_sample["E6"]["reason"])
check("无任务生成", sub2["tasks"] == [])
check("E11 始终未被替选",
      all(t["样本编号"] != "E11" for t in store.rows("reschedule_task")))

# ---------------------------------------------------------------- 5. 调度确认 E9 可用
r = client.post("/api/recovery/receipts/confirm", json={"样本编号": "E9", "usable": True})
conf = r.json()
check("E9 确认可用：核验首份产物、不补采",
      conf["action"] == "held_for_verification" and conf["dispatched"] is False, conf["message"])
e9_row = store.find("failure_quarantine", e9_id)
check("E9 隔离项回填为已恢复", e9_row["status"] == "已恢复", e9_row["status"])
check("E9 处理方式回填", "核验首份产物" in e9_row.get("处理方式", ""))
e9_tasks = [t for t in store.rows("reschedule_task") if t["样本编号"] == "E9"]
check("E9 只有一份核验任务（无补采任务）",
      len(e9_tasks) == 1 and e9_tasks[0]["任务类型"] == "核验",
      str([(t["任务类型"], t["回执"]) for t in e9_tasks]))

# 再次确认：幂等，不能再记一次
r = client.post("/api/recovery/receipts/confirm", json={"样本编号": "E9", "usable": True})
check("E9 二次确认幂等（已计入，不重复记账）",
      r.json().get("already_counted") is True, r.json()["message"])
check("E9 任务仍只有一份",
      len([t for t in store.rows("reschedule_task") if t["样本编号"] == "E9"]) == 1)

# 不可用分支：另一个样本走补采（用 E4 造一笔新回执验证）
client.post("/api/recovery/receipts", json={"样本编号": "E4"})
# 先让 E4 进入待核验申请
e4_id = next(q["id"] for q in store.rows("failure_quarantine") if q["样本编号"] == "E4")
client.post("/api/recovery/reschedule", json={
    "candidate_ids": [e4_id], "来源站点": "站点B", "失败阶段": "采集"})
r = client.post("/api/recovery/receipts/confirm", json={"样本编号": "E4", "usable": False})
check("E4 不可用时派发补采", r.json()["action"] == "dispatched_recrawl"
      and r.json()["dispatched"] is True)
check("E4 补采任务唯一",
      len([t for t in store.rows("reschedule_task") if t["样本编号"] == "E4"]) == 1)

# 已计入原计划的成功（种子 E7）：确认接口必须幂等拒绝，重排不能再记一次
r = client.post("/api/recovery/receipts/confirm", json={"样本编号": "E7", "usable": True})
e7_conf = r.json()
check("E7 成功已计入原计划，确认接口幂等不重复记账",
      e7_conf.get("already_counted") is True, e7_conf.get("message", ""))
check("E7 未因确认产生新任务",
      all(t["样本编号"] != "E7" for t in store.rows("reschedule_task")))
# E7 走正常重排仍可补采（它当前没有待确认回执，与已结账的回执互不影响）
e7_id = next(q["id"] for q in store.rows("failure_quarantine") if q["样本编号"] == "E7")
r = client.post("/api/recovery/reschedule", json={
    "candidate_ids": [e7_id], "来源站点": "站点B", "失败阶段": "采集"})
check("E7 正常重排可入选并只生成一份补采任务",
      r.json()["results"][0]["state"] == "本次入选"
      and len([t for t in store.rows("reschedule_task") if t["样本编号"] == "E7"]) == 1)

# ---------------------------------------------------------------- 6. 已处理身份复校
r = client.post("/api/recovery/reschedule", json={
    "candidate_ids": [e9_id], "来源站点": "站点B", "失败阶段": "采集"})
re_e9 = r.json()["results"][0]
check("曾经可重试现已完成的 E9 提交时标记已处理",
      re_e9["state"] == "已处理", re_e9["reason"])
# 种子已恢复的 E8
e8_id = next(q["id"] for q in store.rows("failure_quarantine") if q["样本编号"] == "E8")
r = client.post("/api/recovery/reschedule", json={"candidate_ids": [e8_id]})
check("已恢复 E8 标记已处理", r.json()["results"][0]["state"] == "已处理")
# 超限 E16
e16_id = next(q["id"] for q in store.rows("failure_quarantine") if q["样本编号"] == "E16")
r = client.post("/api/recovery/reschedule", json={"candidate_ids": [e16_id]})
check("重试超限 E16 标记候选失效", r.json()["results"][0]["state"] == "候选失效")

# ---------------------------------------------------------------- 7. 翻页后阶段变化 + 计划页面核对
# E5 当前在站点B采集候选中；后台把它的重试阶段改为解析
e5_id = next(q["id"] for q in store.rows("failure_quarantine") if q["样本编号"] == "E5")
r = client.post("/api/recovery/stage", json={"quarantine_id": e5_id, "失败阶段": "解析"})
check("阶段变更成功", r.json()["ok"], r.json().get("message", ""))
# 再按 站点B+采集 提交 E5 身份：应因真实阶段变化而失效
r = client.post("/api/recovery/reschedule", json={
    "candidate_ids": [e5_id], "来源站点": "站点B", "失败阶段": "采集"})
check("阶段变化后旧筛选提交 E5 判候选失效",
      r.json()["results"][0]["state"] == "候选失效", r.json()["results"][0]["reason"])
# 按新阶段提交则可入选
r = client.post("/api/recovery/reschedule", json={
    "candidate_ids": [e5_id], "来源站点": "站点B", "失败阶段": "解析"})
check("按真实新阶段提交 E5 可入选",
      r.json()["results"][0]["state"] == "本次入选", r.json()["results"][0]["state"])

# 计划页面核对关联与可恢复范围
plan_id = store.rows("crawl_schedule")[0]["id"]
r = client.get(f"/api/recovery/plans/{plan_id}/scope")
scope = r.json()
check("计划恢复关联 200", r.status_code == 200)
linked_samples = {t["样本编号"] for t in scope["linked_tasks"]}
check("计划已关联 E5/E4 重排任务", {"E4", "E5"} <= linked_samples, str(linked_samples))
check("E9 核验任务与隔离项回填可在计划页核对",
      any(t["样本编号"] == "E9" and t["任务类型"] == "核验"
          and t["隔离项状态"] == "已恢复" for t in scope["linked_tasks"]))
check("计划页给出实际可恢复范围数量",
      scope["recoverable_total"] == len(scope["recoverable_scope"]) > 0)
check("可恢复范围只含当前真实可重排项",
      all(i["候选状态"] == "可重排" for i in scope["recoverable_scope"]))
check("E11 从未进入任何计划任务",
      "E11" not in {t["样本编号"] for t in store.rows("reschedule_task")})

print()
if failures:
    print(f"共 {len(failures)} 项失败：{failures}")
    raise SystemExit(1)
print("全部场景验证通过")
