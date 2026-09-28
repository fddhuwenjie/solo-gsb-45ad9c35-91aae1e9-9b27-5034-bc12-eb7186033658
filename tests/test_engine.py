"""引擎行为验证（标准库，直接 python3 tests/test_engine.py 运行）。"""
import sys, os, itertools
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.engine import solve

DT = 3600.0

def base(**kw):
    p = dict(
        name="t", capacity_m3=80000, initial_storage_m3=2000,
        dt_s=DT, q_safe=10, min_interval=2,
        initial_opening_index=1, initial_held=0,
        openings=[0, 3, 6, 9, 12, 16],
        inflows=[2, 5, 12, 20, 18, 12, 7, 4, 2, 1, 0, 0],
    )
    p.update(kw)
    return p

def water_balance_ok(r):
    v = r["params"]["initial_storage_m3"]
    for s in r["steps"]:
        assert abs(s["storage_start_m3"] - v) < 1e-6
        v2 = v + s["inflow_m3"] - s["release_m3"]
        assert abs(s["storage_end_m3"] - v2) < 1e-6
        assert s["storage_end_m3"] <= r["params"]["capacity_m3"] + 1e-6
        v = v2
    return True

# 1) 示例可行、无溢流、水量平衡
r = solve(base())
assert r["feasible"], r
water_balance_ok(r)
print("case1 feasible, switches=", r["summary"]["switches"],
      "exceed=", r["summary"]["total_exceed_m3"],
      "peakV=", round(r["summary"]["max_storage_m3"]),
      "peakQ=", round(r["summary"]["peak_release_m3s"], 2))

# 2) 现状 1 档且刚切到位 + 间隔 3 -> LOCKED_INTERVAL；减小间隔可解
r2 = solve(base(min_interval=3))
assert not r2["feasible"] and r2["infeasible"]["code"] == "LOCKED_INTERVAL", r2
sug = r2["infeasible"]["suggestions"]
assert any("保持满" in s for s in sug), sug
print("case2:", r2["infeasible"]["title"], "at t=", r2["infeasible"]["at_step"] + 1)
for s in sug: print("   -", s)
# 同时间隔 2 可行
assert solve(base(min_interval=2))["feasible"]
# 现状档已保持足够久（可立即切换）时 K=3 可行
assert solve(base(min_interval=3, initial_held=3))["feasible"]

# 3) 库容/泄流能力不足 -> OVERFLOW_UNAVOIDABLE（即使间隔=1 可立即换档）
r3 = solve(base(min_interval=1, initial_held=1,
                capacity_m3=30000, openings=[0, 3, 6, 9, 12]))
assert not r3["feasible"] and r3["infeasible"]["code"] == "OVERFLOW_UNAVOIDABLE", r3
need = [s for s in r3["infeasible"]["suggestions"] if "泄流能力" in s]
assert need
print("case3:", r3["infeasible"]["code"], "at t=", r3["infeasible"]["at_step"] + 1, "|", need[0])

# 4) 容量极小：扩容量建议出现 + 贪心轨迹返回
r4 = solve(base(min_interval=1, initial_held=1, capacity_m3=5000))
assert not r4["feasible"] and r4["infeasible"]["code"] == "OVERFLOW_UNAVOIDABLE"
assert any("扩大" in s for s in r4["infeasible"]["suggestions"])
assert len(r4["infeasible"]["greedy_trajectory"]) == 12
print("case4: overflow unavoidable w/ tiny capacity OK")

# 5) 收紧安全流量 -> 必然超标但仍可行
r5 = solve(base(q_safe=3))
assert r5["feasible"] and r5["summary"]["total_exceed_m3"] > 0
water_balance_ok(r5)
print("case5: exceed total=", round(r5["summary"]["total_exceed_m3"], 1),
      "steps=", r5["summary"]["exceed_steps"])

# 6) 与暴力枚举对照（含初始档位与保持时间的约束）
def brute(p):
    dt = p["dt_s"]; cap = p["capacity_m3"]; v0 = p["initial_storage_m3"]
    best = None
    g0 = p["initial_opening_index"] - 1
    run = p["initial_held"]
    for seq in itertools.product(range(len(p["openings"])), repeat=len(p["inflows"])):
        v = v0; g = g0; rn = run; ok = True
        exceed = sw = 0; peakR = peakV = 0
        for t, j in enumerate(seq):
            if rn < p["min_interval"] and j != g:
                ok = False; break
            if j != g: sw += 1
            rn = 1 if j != g else rn + 1
            g = j
            rel = min(p["openings"][j] * dt, v + p["inflows"][t] * dt)
            v = v + p["inflows"][t] * dt - rel
            if v > cap + 1e-6: ok = False; break
            exceed += max(0, rel - p["q_safe"] * dt)
            peakR = max(peakR, rel); peakV = max(peakV, v)
        if ok:
            key = (round(exceed, 4), sw, round(peakR, 4), round(peakV, 4))
            if best is None or key < best: best = key
    return best

for p6 in [
    base(inflows=[2, 5, 12, 20, 18, 12], openings=[0, 6, 12, 16],
         capacity_m3=50000, q_safe=8, min_interval=2,
         initial_opening_index=1, initial_held=0),
    base(inflows=[2, 5, 12, 20, 18, 12], openings=[0, 6, 12, 16],
         capacity_m3=70000, q_safe=9, min_interval=3,
         initial_opening_index=3, initial_held=1),
    base(inflows=[0, 3, 8, 6], openings=[0, 4, 8, 12],
         capacity_m3=40000, q_safe=5, min_interval=2,
         initial_opening_index=2, initial_held=2),
]:
    r6 = solve(p6)
    b = brute(p6)
    if b is None:
        assert not r6["feasible"], "brute says infeasible"
        print("case6: both infeasible (", p6["initial_opening_index"], p6["initial_held"], ")")
    else:
        assert r6["feasible"], "brute found feasible"
        got = (round(r6["objective"]["total_exceed_m3"], 4), r6["objective"]["switches"],
               round(r6["objective"]["peak_release_m3"], 4), round(r6["objective"]["peak_storage_m3"], 4))
        assert got == b, (got, b)
        water_balance_ok(r6)
        print("case6: DP matches brute force:", got)

# 7) 单时段边界
r7 = solve(base(inflows=[5], min_interval=5))
assert r7["feasible"] and len(r7["steps"]) == 1
print("case7: single step OK")

# 8) 库空限制：锁定在非零档但无水可供时，实际出流夹到 0（EMPTY 事件，不报错）
r8 = solve(base(initial_storage_m3=0, inflows=[0, 0, 2, 5],
                min_interval=2, initial_opening_index=2, initial_held=0, q_safe=10))
assert r8["feasible"]
assert any(any(e["code"] == "EMPTY" for e in s["events"]) for s in r8["steps"])
water_balance_ok(r8)
print("case8: empty-pool clamping OK")

# 9) 初始库容即超容：立即诊断为 ALREADY_OVERFLOW
r9 = solve(base(initial_storage_m3=90000))
assert not r9["feasible"] and r9["infeasible"]["code"] == "ALREADY_OVERFLOW"
assert r9["infeasible"]["at_step"] == 0
print("case9: initial over-capacity diagnosed at t=1")

# 10) 初始即高档、间隔 1：与"首档自由"等价，可自由提前预泄
r10 = solve(base(min_interval=1, initial_opening_index=6, initial_held=1))
assert r10["feasible"]
water_balance_ok(r10)
print("case10: free initial switching OK, exceed=",
      round(r10["summary"]["total_exceed_m3"], 1))

# 11) 回归：48 时段 × 10 档位场景触发原"前沿>120 点等距抽稀却返回成功"缺陷。
#     旧实现会把前沿抽稀到 120 点并返回超标 342140 m³/6 次切换的近似解冒充最优；
#     无损精确解（已用无支配 DP 独立交叉校验）应为 (341780, 3, 72000, 299880)。
shape48 = [
    3, 4, 6, 9, 13, 18, 24, 30, 36, 41, 45, 47,
    46, 43, 39, 35, 31, 28, 25, 23, 21, 26, 32, 38,
    42, 44, 40, 34, 28, 22, 17, 13, 10, 8, 6, 5,
    9, 14, 20, 26, 30, 31, 28, 24, 19, 14, 10, 6,
]
p11 = base(capacity_m3=300000, initial_storage_m3=5000, q_safe=8,
           min_interval=3, initial_opening_index=1, initial_held=0,
           openings=[0, 2, 4, 6, 8, 10, 13, 16, 20, 24],
           inflows=[round(x * 0.45, 2) for x in shape48])
assert len(p11["inflows"]) == 48 and len(p11["openings"]) == 10
r11 = solve(p11)
assert r11["feasible"], r11
ps11 = r11["prune_stats"]
assert "thinned" not in ps11, "求解器不得再包含抽稀逻辑"
assert ps11["exact"] is True, "返回结果必须标注为精确解"
# 前沿确实超过旧的 120 抽稀阈值 —— 即原缺陷的触发条件
assert ps11["max_frontier_points"] > 120, ps11["max_frontier_points"]
got11 = (round(r11["objective"]["total_exceed_m3"], 4), r11["objective"]["switches"],
         round(r11["objective"]["peak_release_m3"], 4),
         round(r11["objective"]["peak_storage_m3"], 4))
assert got11 == (341780.0, 3, 72000.0, 299880.0), got11
# 旧实现（抽稀后）给出的近似劣解，绝不能再被返回
assert got11 < (342140.0, 6, 57600.0, 299520.0)
water_balance_ok(r11)
assert len(r11["steps"]) == 48
# 切换锁定：每个非首档位都必须满足已保持时长
_g, _rn = 0, 0
for s in r11["steps"]:
    j = s["opening_index"]
    assert _rn >= p11["min_interval"] or j == _g, "切换锁定被破坏"
    _rn = 1 if j != _g else _rn + 1
    _g = j
# 回溯序列与目标一致
assert r11["summary"]["switches"] == 3
assert abs(r11["summary"]["total_exceed_m3"] - 341780.0) < 1e-6
print("case11: 48x10 EXACT optimum (no thinning), frontier max =",
      ps11["max_frontier_points"], "objective =", got11)

# 12) 回归：小规模（4×2）下旧"仅按超标量支配"的有损剪枝反例，对穷举最优。
#     超标量并列（均为 0）时旧规则误删切换次数更少的路径，返回 (0,1,15480,14760)；
#     穷举最优为 (0,0,6480,14760)（全程 1 档即可）。
p12 = base(capacity_m3=31970.0, initial_storage_m3=2479.0, dt_s=3600, q_safe=8.2,
           min_interval=1, initial_opening_index=1, initial_held=0,
           openings=[1.8, 9.9], inflows=[1.7, 1.0, 5.9, 0.2])
r12 = solve(p12)
assert r12["feasible"]
assert brute(p12) == (0.0, 0, 6480.0, 14760.0)
got12 = (round(r12["objective"]["total_exceed_m3"], 4), r12["objective"]["switches"],
         round(r12["objective"]["peak_release_m3"], 4),
         round(r12["objective"]["peak_storage_m3"], 4))
assert got12 == brute(p12), (got12, brute(p12))
water_balance_ok(r12)
print("case12: lossy-dominance discriminator matches brute force:", got12)

# 13) 随机小规模批量穷举校验（n<=6、m<=3），覆盖各类初始档/保持时长/间隔
import random as _random
_rng = _random.Random(20260922)
for _i in range(40):
    n13 = _rng.randint(3, 6)
    m13 = _rng.randint(2, 3)
    K13 = _rng.randint(1, 3)
    p13 = base(
        capacity_m3=round(_rng.uniform(15000, 60000), 0),
        initial_storage_m3=round(_rng.uniform(0, 8000), 0),
        dt_s=3600, q_safe=round(_rng.uniform(2, 12), 1),
        min_interval=K13,
        initial_opening_index=_rng.randrange(m13) + 1,
        initial_held=_rng.randint(0, K13),
        openings=sorted(set(round(_rng.uniform(0, 14), 1) for _ in range(m13))),
        inflows=[round(_rng.uniform(0, 18), 1) for _ in range(n13)],
    )
    # sorted(set(...)) 可能使档位数 < m13，修正初始档越界
    p13["initial_opening_index"] = min(p13["initial_opening_index"],
                                       len(p13["openings"]))
    r13 = solve(p13)
    b13 = brute(p13)
    if b13 is None:
        assert not r13["feasible"], (_i, p13, "brute says infeasible")
    else:
        assert r13["feasible"], (_i, p13, "brute found feasible")
        got13 = (round(r13["objective"]["total_exceed_m3"], 4),
                 r13["objective"]["switches"],
                 round(r13["objective"]["peak_release_m3"], 4),
                 round(r13["objective"]["peak_storage_m3"], 4))
        assert got13 == b13, (_i, got13, b13, p13)
        water_balance_ok(r13)
print("case13: 40 random small cases all match brute force")

# 14) 资源保护上限被触及时，必须返回可识别的规模拒绝（SEARCH_LIMIT），
#     不能崩溃、不能返回近似/可行结果。
import app.engine as _engine
_old_limit = _engine.DEFAULT_MAX_STATES
_engine.DEFAULT_MAX_STATES = 10          # 极小单层上限，强制快速触限
try:
    r14 = solve(base())
finally:
    _engine.DEFAULT_MAX_STATES = _old_limit
assert not r14["feasible"]
assert r14["infeasible"]["code"] == "SEARCH_LIMIT", r14
assert r14["infeasible"]["at_step"] is not None
assert len(r14["infeasible"]["suggestions"]) >= 2
assert "近似" in r14["infeasible"]["message"]
assert "steps" not in r14 and "objective" not in r14   # 拒绝时不带任何序列/最优值
print("case14: SEARCH_LIMIT scale rejection at step",
      r14["infeasible"]["at_step"] + 1)

# 15) 触限恢复后再次正常求解：上限改回后结果不受影响、仍为精确解
r15 = solve(base())
assert r15["feasible"] and r15["prune_stats"]["exact"] is True
print("case15: solve works after limit restored")

# 16) 高精度回归（本缺陷的原始触发输入）：容量 99999.99998、初库容 99999.99994、
#     步长 60s、安全流量 0、初始零档已解锁、单时段入流 0.000001 m³/s、
#     档位 0 / 0.000001 m³/s。零档末库容 = 100000.0 m³ > 容量（超 2e-5 m³），
#     旧实现按 1e-4 m³ 归并把初库容舍小到 99999.9999 而误判零档可行；
#     精确整数表示下必须选择非零档，且末库容恰为 99999.99994 m³。
from fractions import Fraction

p16 = dict(name="precision", capacity_m3=99999.99998, initial_storage_m3=99999.99994,
           dt_s=60, q_safe=0, min_interval=1,
           initial_opening_index=1, initial_held=1,
           openings=[0, 0.000001], inflows=[0.000001])
# 先确认：按原始参数精确重算，零档确实超容（缺陷前提成立）
assert Fraction("99999.99994") + Fraction("0.000001") * 60 > Fraction("99999.99998")
r16 = solve(p16)
assert r16["feasible"], r16
assert [s["opening_index"] for s in r16["steps"]] == [1], "必须选择非零档"
assert r16["summary"]["switches"] == 1
assert r16["summary"]["final_storage_m3"] == 99999.99994
assert r16["summary"]["max_storage_m3"] <= r16["params"]["capacity_m3"]
assert r16["objective"]["total_exceed_m3"] == 0.00006      # 安全流量为 0，全部出流超标
assert r16["objective"]["peak_release_m3"] == 0.00006
assert r16["objective"]["peak_storage_m3"] == 99999.99994
water_balance_ok(r16)
# 变体 a：只有零档 -> 物理无解（OVERFLOW_UNAVOIDABLE），绝不可标可行
r16a = solve(dict(p16, openings=[0]))
assert not r16a["feasible"] and r16a["infeasible"]["code"] == "OVERFLOW_UNAVOIDABLE", r16a
# 变体 b：非零档存在但初始零档被锁定（held=0 < K=2）-> 约束型无解
r16b = solve(dict(p16, min_interval=2, initial_held=0))
assert not r16b["feasible"] and r16b["infeasible"]["code"] == "LOCKED_INTERVAL", r16b
print("case16: high-precision input must pick non-zero gear;",
      "final =", r16["summary"]["final_storage_m3"], "<= cap =",
      r16["params"]["capacity_m3"])

# 17) 精确边界：末库容 == 容量 合法；超 1 个量子即溢流；初库容 == 容量 合法
p17 = dict(name="edge-eq", capacity_m3=100000.00002, initial_storage_m3=99999.99996,
           dt_s=60, q_safe=0, min_interval=1, initial_opening_index=1, initial_held=1,
           openings=[0], inflows=[0.000001])          # 末库容恰为 100000.00002 == 容量
r17 = solve(p17)
assert r17["feasible"] and r17["summary"]["final_storage_m3"] == 100000.00002, r17
r17b = solve(dict(p17, initial_storage_m3=99999.99997))   # 末库容超容量 1e-5 m³
assert not r17b["feasible"] and r17b["infeasible"]["code"] == "OVERFLOW_UNAVOIDABLE"
r17c = solve(dict(p17, initial_storage_m3=100000.00002,   # 初库容 == 容量：不判 ALREADY_OVERFLOW
                  openings=[0.000001]))
assert r17c["feasible"] and r17c["summary"]["final_storage_m3"] == 100000.00002
r17d = solve(dict(p17, initial_storage_m3=100000.00003))  # 初库容超 1 个量子
assert not r17d["feasible"] and r17d["infeasible"]["code"] == "ALREADY_OVERFLOW"
# 小数步长 + 微量流量：dt=2.5s、入流 0.000002 m³/s -> 单时段 5e-6 m³ 量子；
# 容量 == 初库容，零档首时段末库容 50000.00001 > 容量 50000.000005，必须选非零档
p17e = dict(name="edge-dt", capacity_m3=50000.000005, initial_storage_m3=50000.000005,
            dt_s=2.5, q_safe=0, min_interval=1, initial_opening_index=1, initial_held=1,
            openings=[0, 0.000002], inflows=[0.000002, 0.000002])
r17e = solve(p17e)
assert r17e["feasible"] and [s["opening_index"] for s in r17e["steps"]] == [1, 1]
assert r17e["summary"]["final_storage_m3"] == 50000.000005
water_balance_ok(r17e)
print("case17: exact capacity boundary (== ok, +1 quantum overflow) OK")

# 18) 高精度随机小规模穷举对照：容量/库容 5 位小数、流量 1e-6 m³/s 级、
#     小数步长；暴力枚举用 Fraction 精确有理数（独立于引擎的另一套实现）。
def brute_exact(p):
    dt = Fraction(str(p["dt_s"])); cap = Fraction(str(p["capacity_m3"]))
    v0 = Fraction(str(p["initial_storage_m3"]))
    o_step = [Fraction(str(o)) * dt for o in p["openings"]]
    i_step = [Fraction(str(i)) * dt for i in p["inflows"]]
    qs = Fraction(str(p["q_safe"])) * dt
    best = None
    g0 = p["initial_opening_index"] - 1
    for seq in itertools.product(range(len(o_step)), repeat=len(i_step)):
        v = v0; g = g0; rn = p["initial_held"]; ok = True
        e = Fraction(0); sw = 0; pr = Fraction(0); pv = Fraction(0)
        for t, j in enumerate(seq):
            if rn < p["min_interval"] and j != g:
                ok = False; break
            if j != g: sw += 1
            rn = 1 if j != g else rn + 1
            g = j
            avail = v + i_step[t]
            rel = min(o_step[j], avail)
            v = avail - rel
            if v > cap: ok = False; break          # 精确容量判定
            if rel > qs: e += rel - qs
            if rel > pr: pr = rel
            if v > pv: pv = v
        if ok:
            key = (e, sw, pr, pv)
            if best is None or key < best: best = key
    return best

def replay_exact(p, steps):
    """按原始参数以 Fraction 精确回放返回的档位序列：水量平衡与容量须严格成立。"""
    dt = Fraction(str(p["dt_s"])); cap = Fraction(str(p["capacity_m3"]))
    v = Fraction(str(p["initial_storage_m3"]))
    for s in steps:
        o = Fraction(str(p["openings"][s["opening_index"]])) * dt
        i = Fraction(str(p["inflows"][s["t"]])) * dt
        assert Fraction(str(s["storage_start_m3"])) == v, "时段间库容不衔接"
        rel = min(o, v + i)
        v = v + i - rel
        assert v <= cap, "精确回放超容"
        assert Fraction(str(s["storage_end_m3"])) == v, "末库容与精确回放不一致"
        assert Fraction(str(s["release_m3"])) == rel

_rng2 = _random.Random(20260927)
for _i in range(30):
    n18 = _rng2.randint(1, 5)
    m18 = _rng2.randint(1, 3)
    K18 = _rng2.randint(1, 3)
    openings18 = sorted({_rng2.randint(0, 40) / 1e6 for _ in range(m18)})  # ≤4e-5 m³/s
    cap_q18 = _rng2.randint(9_000_000_000, 11_000_000_000)     # 90000~110000，5 位小数
    v0_q18 = cap_q18 - _rng2.randint(0, 2000)                  # 贴近满库（≤0.02 m³ 余量）
    p18 = dict(
        name="hp", capacity_m3=cap_q18 / 1e5, initial_storage_m3=v0_q18 / 1e5,
        dt_s=_rng2.choice([60, 120, 2.5]), q_safe=_rng2.randint(0, 20) / 1e6,
        min_interval=K18,
        initial_opening_index=_rng2.randrange(len(openings18)) + 1,
        initial_held=_rng2.randint(0, K18),
        openings=openings18,
        inflows=[_rng2.randint(0, 30) / 1e6 for _ in range(n18)],
    )
    r18 = solve(p18)
    b18 = brute_exact(p18)
    if b18 is None:
        assert not r18["feasible"], (_i, p18, "brute says infeasible")
    else:
        assert r18["feasible"], (_i, p18, "brute found feasible")
        got18 = (r18["objective"]["total_exceed_m3"], r18["objective"]["switches"],
                 r18["objective"]["peak_release_m3"], r18["objective"]["peak_storage_m3"])
        want18 = (float(b18[0]), b18[1], float(b18[2]), float(b18[3]))
        assert got18 == want18, (_i, got18, want18, p18)
        replay_exact(p18, r18["steps"])
        water_balance_ok(r18)
print("case18: 30 high-precision random cases match exact Fraction brute force")

# 19) 高精度 + 切换锁定混合回归：锁定期间夹档/超容判定均须精确
p19 = dict(name="hp-lock", capacity_m3=60000.00003, initial_storage_m3=59999.99999,
           dt_s=60, q_safe=0.000001, min_interval=2,
           initial_opening_index=3, initial_held=0,
           openings=[0, 0.000001, 0.000003],
           inflows=[0.000002, 0.000002, 0.000001, 0.000003])
r19 = solve(p19)
b19 = brute_exact(p19)
assert b19 is not None and r19["feasible"]
got19 = (r19["objective"]["total_exceed_m3"], r19["objective"]["switches"],
         r19["objective"]["peak_release_m3"], r19["objective"]["peak_storage_m3"])
assert got19 == (float(b19[0]), b19[1], float(b19[2]), float(b19[3])), (got19, b19)
replay_exact(p19, r19["steps"])
water_balance_ok(r19)
print("case19: high-precision + switch-lock matches exact brute force:", got19)

print("\nALL ENGINE TESTS PASSED")
