"""回归校验：通用模块、旧隔离服务动作、新隔离字段必填校验、失败计划重排落库。"""
from fastapi.testclient import TestClient

from app.main import app
from app.store import store

client = TestClient(app)
fails = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        fails.append(name)


# 通用模块列表不受影响
r = client.get("/api/shop_site", params={"page": 1, "size": 5})
check("通用模块 shop_site 正常", r.status_code == 200 and r.json()["total"] >= 3)
r = client.get("/api/crawl_schedule")
check("crawl_schedule 列表正常", r.status_code == 200 and r.json()["total"] == 3)

# 隔离区新字段完整返回
r = client.get("/api/failure_quarantine", params={"size": 1})
row = r.json()["items"][0]
for field in ("样本编号", "来源站点", "站点标识", "失败阶段"):
    check(f"隔离项含字段 {field}", field in row and row[field])

# 必填校验：缺来源站点应被拒
r = client.post("/api/failure_quarantine", json={"values": {"样本指纹": "X", "失败阶段": "采集", "失败原因": "y"}})
check("缺来源站点/站点标识登记被拒", r.json()["ok"] is False)

# 完整登记成功
before = r.json() if False else client.get("/api/failure_quarantine").json()["total"]
r = client.post("/api/failure_quarantine", json={"values": {
    "样本编号": "E99", "样本指纹": "FP-E99", "来源站点": "站点A", "站点标识": "SITE-A",
    "失败阶段": "采集", "失败原因": "回归测试", "隔离时间": "2026-11-26T00:00:00Z", "重试次数": "0"}})
check("完整字段登记成功", r.json()["ok"] is True, r.json().get("message", ""))
after = client.get("/api/failure_quarantine").json()["total"]
check("登记后总数 +1", after == before + 1)

# 旧动作规则：立即重试 -> 重试中
new_id = r.json()["entry"]["id"]
r = client.post(f"/api/failure_quarantine/{new_id}/actions", json={"action": "立即重试"})
check("立即重试流转到重试中", r.json()["entry"]["status"] == "重试中", r.json()["entry"]["status"])

# 失败计划重排生成的隔离项带真实来源与阶段
plan = store.rows("crawl_schedule")[0]
plan["status"] = "执行失败"
from app.services import service_for
res = service_for("crawl_schedule").reschedule_failed(plan["id"])
check("失败计划重排成功", res["ok"], res.get("message", ""))
q = store.find("failure_quarantine", res["quarantine_id"])
check("重排隔离项带失败阶段=调度", q["失败阶段"] == "调度")
check("重排隔离项带来源字段", "来源站点" in q and "站点标识" in q and "样本编号" in q)
# 该隔离项能被恢复候选按阶段精确筛出
cand = client.get("/api/recovery/candidates", params={"失败阶段": "调度"}).json()
check("调度阶段隔离项进入恢复候选", any(i["id"] == q["id"] for i in cand["items"]))

print()
print("回归", "全部通过" if not fails else f"失败 {fails}")
raise SystemExit(1 if fails else 0)
