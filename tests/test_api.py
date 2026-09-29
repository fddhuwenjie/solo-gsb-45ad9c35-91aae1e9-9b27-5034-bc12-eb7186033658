"""端到端 API 验证：需 fastapi/httpx，运行：
   PYTHONPATH=/tmp/pylibs:. BASIN_DB_PATH=/tmp/e2e_basin.db python3 tests/test_api.py"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_db = tempfile.mktemp(suffix=".db")
os.environ["BASIN_DB_PATH"] = _db
if os.path.exists(_db):
    os.remove(_db)

from fastapi.testclient import TestClient
from app.main import app
from app import database

database.init_db()
client = TestClient(app)

DEMO_PARAMS = dict(
    name="12h 暴雨", capacity_m3=80000, initial_storage_m3=2000, dt_s=3600,
    q_safe=12, min_interval=2, initial_opening_index=1, initial_held=0,
    openings=[0, 3, 6, 9, 12, 16],
    inflows=[2, 5, 12, 20, 18, 12, 7, 4, 2, 1, 0, 0],
)

# 健康检查
assert client.get("/api/health").json() == {"ok": True}

# 页面
assert client.get("/").status_code == 200
assert client.get("/static/app.js").status_code == 200

# 即席推演：可行 + 零超标
r = client.post("/api/solve", json=DEMO_PARAMS).json()
assert r["feasible"], r
assert r["summary"]["total_exceed_m3"] == 0.0
assert r["params"]["initial_opening_index"] == 1
assert len(r["steps"]) == 12

# 即席推演：切换锁定无解
bad = dict(DEMO_PARAMS, min_interval=3)
r2 = client.post("/api/solve", json=bad).json()
assert not r2["feasible"] and r2["infeasible"]["code"] == "LOCKED_INTERVAL"
assert r2["infeasible"]["at_step"] is not None and len(r2["infeasible"]["suggestions"]) >= 2

# 即席推演：物理无解（满管也溢流）
phys = dict(DEMO_PARAMS, min_interval=1, initial_held=1, capacity_m3=30000,
            openings=[0, 3, 6, 9, 12])
r3 = client.post("/api/solve", json=phys).json()
assert not r3["feasible"] and r3["infeasible"]["code"] == "OVERFLOW_UNAVOIDABLE"
assert any("提高" in s for s in r3["infeasible"]["suggestions"])
assert len(r3["infeasible"]["greedy_trajectory"]) == 12

# 即席推演：高精度输入不得被库容归并舍入误判可行（超 2e-5 m³ 也算超容）。
# 零档末库容 = 99999.99994 + 0.000001*60 = 100000.0 > 容量 99999.99998，
# 必须选择非零档，末库容恰为 99999.99994 m³。
prec = dict(DEMO_PARAMS, name="高精度临界", capacity_m3=99999.99998,
            initial_storage_m3=99999.99994, dt_s=60, q_safe=0,
            min_interval=1, initial_opening_index=1, initial_held=1,
            openings=[0, 0.000001], inflows=[0.000001])
rp = client.post("/api/solve", json=prec).json()
assert rp["feasible"], rp
assert [s["opening_index"] for s in rp["steps"]] == [1]
assert rp["summary"]["final_storage_m3"] == 99999.99994
assert rp["summary"]["max_storage_m3"] <= rp["params"]["capacity_m3"]
assert rp["prune_stats"]["exact"] is True
# 超容变体（仅零档）：必须判不可行，不得标精确可行
rp2 = client.post("/api/solve", json=dict(prec, openings=[0])).json()
assert not rp2["feasible"] and rp2["infeasible"]["code"] == "OVERFLOW_UNAVOIDABLE"
# 持久化推演同样精确（场景保存/历史回放路径）
sid_p = client.post("/api/scenarios", json=prec).json()["id"]
run_p = client.post(f"/api/scenarios/{sid_p}/run").json()
assert run_p["feasible"] and run_p["summary"]["final_storage_m3"] == 99999.99994
hist_p = client.get(f"/api/runs/{run_p['run_id']}").json()
assert hist_p["summary"]["max_storage_m3"] <= hist_p["params"]["capacity_m3"]
client.delete(f"/api/scenarios/{sid_p}")

# 即席推演：规模超出资源保护上限 -> 可识别的规模拒绝（不返回近似/可行结果）
from app import engine
_old_limit = engine.DEFAULT_MAX_STATES
engine.DEFAULT_MAX_STATES = 5
try:
    r_limit = client.post("/api/solve", json=DEMO_PARAMS).json()
finally:
    engine.DEFAULT_MAX_STATES = _old_limit
assert not r_limit["feasible"], r_limit
assert r_limit["infeasible"]["code"] == "SEARCH_LIMIT"
assert r_limit["infeasible"]["at_step"] is not None
assert "steps" not in r_limit and "objective" not in r_limit
assert "近似" in r_limit["infeasible"]["message"]
assert len(r_limit["infeasible"]["suggestions"]) >= 2
# 拒绝后服务仍可正常推演（上限已恢复、结果精确）
r_after = client.post("/api/solve", json=DEMO_PARAMS).json()
assert r_after["feasible"] and r_after["prune_stats"]["exact"] is True

# 参数校验：初始档位越界 -> 422
bad_params = dict(DEMO_PARAMS, initial_opening_index=99)
assert client.post("/api/solve", json=bad_params).status_code == 422
# 负容量 -> 422
assert client.post("/api/solve", json=dict(DEMO_PARAMS, capacity_m3=-1)).status_code == 422

# 场景 CRUD + 持久化推演
sid = client.post("/api/scenarios", json=DEMO_PARAMS).json()["id"]
run = client.post(f"/api/scenarios/{sid}/run").json()
assert run["feasible"] and run["run_id"]
lst = client.get("/api/scenarios").json()
assert any(s["id"] == sid and s["last_feasible"] == 1 for s in lst)
runs = client.get(f"/api/scenarios/{sid}/runs").json()
assert len(runs) == 1
# 取回历史结果
hist = client.get(f"/api/runs/{run['run_id']}").json()
assert hist["summary"]["total_exceed_m3"] == 0

# 更新参数后重算（K=3 无解），最新记录翻转
client.put(f"/api/scenarios/{sid}", json=bad)
run2 = client.post(f"/api/scenarios/{sid}/run").json()
assert not run2["feasible"]
s = client.get(f"/api/scenarios/{sid}").json()
assert s["params"]["min_interval"] == 3
assert len(s["runs"]) == 2 and s["runs"][0]["feasible"] is False  # 最新在前

# 载入场景详情
assert client.get(f"/api/scenarios/{sid}").status_code == 200
assert client.get("/api/scenarios/99999").status_code == 404

# 删除场景级联
client.delete(f"/api/scenarios/{sid}")
assert client.get(f"/api/runs/{run['run_id']}").status_code == 404

# ---------------- 容量反算（POST /api/size-capacity） ----------------
SIZE_PARAMS = dict(
    name="容量反算", capacity_upper_m3=120000, exceed_budget_m3=0,
    initial_storage_m3=2000, dt_s=3600, q_safe=12, min_interval=2,
    initial_opening_index=1, initial_held=0,
    openings=[0, 3, 6, 9, 12, 16],
    inflows=[2, 5, 12, 20, 18, 12, 7, 4, 2, 1, 0, 0],
)

# 基本反算：最小容量、夹逼区间、见证序列
rs = client.post("/api/size-capacity", json=SIZE_PARAMS).json()
assert rs["feasible"], rs
assert rs["kind"] == "capacity_sizing"
cap_star = rs["min_capacity_m3"]
ss = rs["search"]
assert ss["proven_feasible_at_m3"] == cap_star
assert abs(ss["proven_infeasible_below_m3"] - (cap_star - ss["quantum_m3"])) < 1e-12
assert ss["interval_width_m3"] == ss["quantum_m3"] == 1.0
assert ss["exact"] is True and ss["monotonic"] is True
assert len(ss["trials"]) == ss["iterations"] + 1      # 上界检验 + 每轮二分各一次
ws = rs["witness"]
assert ws["params"]["capacity_m3"] == cap_star
assert len(ws["steps"]) == 12
assert ws["summary"]["total_exceed_m3"] <= SIZE_PARAMS["exceed_budget_m3"]
assert ws["summary"]["max_storage_m3"] <= cap_star
# 见证序列水量平衡复核
_v = SIZE_PARAMS["initial_storage_m3"]
for _st in ws["steps"]:
    assert abs(_st["storage_start_m3"] - _v) < 1e-6
    _v = _v + _st["inflow_m3"] - _st["release_m3"]
    assert abs(_st["storage_end_m3"] - _v) < 1e-6
    assert _v <= cap_star + 1e-9
# 与 /api/solve 交叉验证：最小容量处可行且达标；低 1 个量子处不可行或不达标
r_hi = client.post("/api/solve", json=dict(DEMO_PARAMS, capacity_m3=cap_star)).json()
assert r_hi["feasible"] and r_hi["objective"]["total_exceed_m3"] <= 0
r_lo = client.post("/api/solve", json=dict(DEMO_PARAMS, capacity_m3=cap_star - 1)).json()
assert (not r_lo["feasible"]) or r_lo["objective"]["total_exceed_m3"] > 0

# 上界不可行：预算过紧（BUDGET_UNATTAINABLE）
rs2 = client.post("/api/size-capacity",
                  json=dict(SIZE_PARAMS, q_safe=0, exceed_budget_m3=0)).json()
assert not rs2["feasible"] and rs2["infeasible"]["code"] == "BUDGET_UNATTAINABLE"
assert rs2["infeasible"]["min_exceed_at_upper_m3"] > 0
assert any("预算" in s or "上界" in s for s in rs2["infeasible"]["suggestions"])
# 上界不可行：物理溢流（OVERFLOW_UNAVOIDABLE，带满管轨迹）
rs3 = client.post("/api/size-capacity",
                  json=dict(SIZE_PARAMS, capacity_upper_m3=5000,
                            exceed_budget_m3=10**9)).json()
assert not rs3["feasible"] and rs3["infeasible"]["code"] == "OVERFLOW_UNAVOIDABLE"
assert len(rs3["infeasible"]["greedy_trajectory"]) == 12
# 上界不可行：初始库容超上界（ALREADY_OVERFLOW）
rs4 = client.post("/api/size-capacity",
                  json=dict(SIZE_PARAMS, capacity_upper_m3=1000)).json()
assert not rs4["feasible"] and rs4["infeasible"]["code"] == "ALREADY_OVERFLOW"
# 上界不可行：切换锁定（LOCKED_INTERVAL）
rs5 = client.post("/api/size-capacity",
                  json=dict(SIZE_PARAMS, capacity_upper_m3=80000,
                            min_interval=3, exceed_budget_m3=10**9)).json()
assert not rs5["feasible"] and rs5["infeasible"]["code"] == "LOCKED_INTERVAL"
assert rs5["infeasible"]["at_step"] is not None

# 任一试算触发 SEARCH_LIMIT -> 整个请求规模拒绝（不得返回不可行/近似结果）
_old_limit2 = engine.DEFAULT_MAX_STATES
engine.DEFAULT_MAX_STATES = 5
try:
    rs6 = client.post("/api/size-capacity", json=SIZE_PARAMS).json()
finally:
    engine.DEFAULT_MAX_STATES = _old_limit2
assert not rs6["feasible"] and rs6["infeasible"]["code"] == "SEARCH_LIMIT"
assert "min_capacity_m3" not in rs6 and "witness" not in rs6
# 恢复后反算正常
rs7 = client.post("/api/size-capacity", json=SIZE_PARAMS).json()
assert rs7["feasible"] and rs7["search"]["exact"] is True

# 参数校验：负预算 / 非正上界 / 初始档越界 -> 422
assert client.post("/api/size-capacity",
                   json=dict(SIZE_PARAMS, exceed_budget_m3=-1)).status_code == 422
assert client.post("/api/size-capacity",
                   json=dict(SIZE_PARAMS, capacity_upper_m3=0)).status_code == 422
assert client.post("/api/size-capacity",
                   json=dict(SIZE_PARAMS, initial_opening_index=99)).status_code == 422

# 高精度反算：量子级精确的最小容量与夹逼下界
rsp = client.post("/api/size-capacity", json=dict(
    name="高精度反算", capacity_upper_m3=99999.99998, exceed_budget_m3=0.00006,
    initial_storage_m3=99999.99994, dt_s=60, q_safe=0, min_interval=1,
    initial_opening_index=1, initial_held=1,
    openings=[0, 0.000001], inflows=[0.000001])).json()
assert rsp["feasible"], rsp
assert rsp["min_capacity_m3"] == 99999.99994
assert rsp["search"]["quantum_m3"] == 1e-05
assert rsp["search"]["proven_infeasible_below_m3"] == 99999.99993
assert rsp["witness"]["summary"]["total_exceed_m3"] == 0.00006
# 预算差 1 个量子 -> BUDGET_UNATTAINABLE
rsp2 = client.post("/api/size-capacity",
                   json=dict(rsp["params"], exceed_budget_m3=0.00005)).json()
assert not rsp2["feasible"] and rsp2["infeasible"]["code"] == "BUDGET_UNATTAINABLE"
assert rsp2["infeasible"]["min_exceed_at_upper_m3"] == 0.00006

os.remove(_db)
print("ALL API E2E TESTS PASSED")
