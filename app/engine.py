"""城市雨洪调蓄池 —— 闸门泄放序列推演引擎。

时段水量平衡（t = 0,1,...）：

    V(t+1) = V(t) + I(t) - R(t)
    R(t)   = min( 闸门档位出流能力, V(t) + I(t) )      # 库空时不能放出没有的水

硬约束（按优先级）：
1. 任何时段末库容 V(t+1) 不得超过调蓄池容量（绝对避免溢流）；
2. 闸门每次切换到新档位后，新档位必须至少连续保持 min_interval 个时段
   （首个档位的选择不受限；min_interval=1 等价于无切换限制）。

优化目标（在全部不溢流的可行序列中按字典序比较）：
    ① 下游安全流量超标总量  Σ max(0, R(t) - Q_safe·dt)   最小
    ② 闸门切换次数最少
    ③ 峰值出流最小
    ④ 峰值库容最小

库容状态的精确表示：接口参数都是有限十进制（流量 m³/s、库容 m³、步长 s）。
求解前把容量、初始库容、各单时段水量（流量×步长）统一换算到"输入精度
自适应的十进制量子"（10^-D m³，D 取上述各量的最大小数位）下的**精确整数**；
此后水量平衡、容量判定、状态归并（归并键 = 精确库容本身）、Pareto 支配
比较与回溯全部在整数域进行，对接口允许的任意精度零舍入。没有固定放大
倍数、也没有任何容差：超容哪怕只有一个量子（例如 2e-5 m³）都会被判为
溢流，末库容恰好等于容量则合法。

搜索方法：以 (当前档位, 已连续保持档位数, 时段末库容) 为状态做动态规划，
同 (档位, 保持时长) 桶内做**无损 Pareto 支配剪枝**（见 _dominance_filter
的证明注释），前沿完整保留、绝不抽稀，因此返回的开度序列一定是字典序
意义下的严格最优解。唯一非最优出口是资源保护：单层/累计/原始状态数触及
DEFAULT_MAX_* 上限时抛出 SearchLimitExceeded，由 solve() 转为可识别的
"规模拒绝"（code=SEARCH_LIMIT），绝不把近似值标成最优。

容量反算（size_capacity，给方案设计人员用）：沿用上述全部入力，只把
"容量"换成待求量 —— 给定超标预算 B 与设计容量上界 U，在初始库容/上界/
预算/各单时段水量共同确定的十进制量子网格 [V0, U] 上，二分求
"全程不溢流且 e*(C)≤B"的最小容量 C*（可行性谓词关于 C 单调，见
size_capacity 段注释的证明）；C* 上的见证序列就是同一套 DP 的字典序
最优解。任一试算触及 SearchLimitExceeded 都意味着该容量真值未知，
整请求直接规模拒绝——未完成试算绝不允许被当作不可行去收缩区间。

单位：流量 m³/s，库容/超标水量 m³，步长 dt 秒；引擎内部一律为整数量子。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

# 精确求解的资源保护上限。触及任一上限即规模拒绝（SEARCH_LIMIT），
# 不做任何前沿抽稀——宁可明确拒绝，也不返回"看似最优"的近似解。
# 量级按单保留状态 ~160B 内存标定：生成期瞬时内存 ≈ (累计保留状态
# ＋单层原始状态) × 160B；下列取值把进程峰值控制在 ~470MB，使求解进程
# 在 1GB 内存容器（含其他进程基线占用）中也不会被 OOM 杀死。
# 典型 48 时段 × 10 档场景累计状态约 1.25M、单层原始状态约 3 万，远低于上限。
DEFAULT_MAX_STATES = 300_000        # 单层（剪枝后）DP 状态数上限
DEFAULT_MAX_TOTAL_STATES = 1_800_000   # 累计保留状态数上限（回溯需保留全部层）
# 单层剪枝前原始状态数上限（生成期瞬时内存保护；原始层最大可达 上层状态数×档位数）
DEFAULT_MAX_RAW_STATES = 1_000_000


class SearchLimitExceeded(Exception):
    def __init__(self, limit: int, step: int):
        self.limit = limit
        self.step = step
        super().__init__(f"搜索空间在第 {step + 1} 时段超过 {limit} 个状态上限")


@dataclass
class SearchResult:
    found: bool
    chosen: Optional[list[int]]
    cost: Optional[tuple]
    prune: dict


# ---------------- 精确整数表示 ----------------

def _dec(x: float) -> Decimal:
    """接口浮点参数 → 用户录入的精确十进制值。

    JSON 文本 "99999.99998" 被解析为最近的二进制浮点，str() 的 shortest-repr
    再无损还原回该十进制串，因此这里拿到的是用户实际录入的精确值。
    """
    return Decimal(str(x))


def _frac_digits(d: Decimal) -> int:
    """有限十进制的小数位数（去尾零后）；整数为 0。"""
    exp = d.normalize().as_tuple().exponent
    return -exp if exp < 0 else 0


def _to_int_quantum(capacity, v0, dt_s, q_safe, openings, inflows,
                    extra_m3: Optional[list] = None):
    """全部浮点参数 → 统一十进制量子下的精确整数表示。

    量子 = 10^-D m³，D 取 容量 / 初始库容 / 各单时段水量（流量×步长，含安全
    流量）（以及容量反算时追加的 m³ 量：容量上界、超标预算）的最大小数位。
    所有输入都是有限十进制，故换算后每个量都是精确整数：
    归并键就是精确库容本身，容量判定是严格整数比较，不存在任何舍入窗口。

    返回 (scale, cap_q, v0_q, qsafe_step_q, openings_step_q, inflows_step_q,
          extras_q)，物理量(m³) = 整数量 / scale；extras_q 为 extra_m3 中
    各量在同一量子下的精确整数（默认空表，保持旧调用的 6 元组语义）。
    """
    dt_d = _dec(dt_s)
    cap_d = _dec(capacity)
    v0_d = _dec(v0)
    qs_step_d = _dec(q_safe) * dt_d
    o_step_d = [_dec(o) * dt_d for o in openings]
    i_step_d = [_dec(i) * dt_d for i in inflows]
    extra_d = [_dec(x) for x in (extra_m3 or [])]
    digits = 0
    for d in (cap_d, v0_d, qs_step_d, *o_step_d, *i_step_d, *extra_d):
        fd = _frac_digits(d)
        if fd > digits:
            digits = fd
    scale = 10 ** digits

    def q(d: Decimal) -> int:
        return int(d * scale)          # d 的小数位 ≤ D，乘积必为整数，转换精确

    return (scale, q(cap_d), q(v0_d), q(qs_step_d),
            [q(x) for x in o_step_d], [q(x) for x in i_step_d],
            [q(x) for x in extra_d])


def _qfmt(q: int, scale: int) -> str:
    """整数量子 → 精确十进制分位字符串（诊断消息用；除以 10^k 十进制下精确）。"""
    return format(Decimal(q) / Decimal(scale), ",f")


def _dominance_filter(bucket: dict) -> dict:
    """同 (档位, 保持时长) 桶内的无损 Pareto 支配剪枝（绝不抽稀）。

    支配规则：桶内状态 A=(v_A, c_A) 支配 B=(v_B, c_B)，当且仅当
        v_A <= v_B  且  代价元组 c_A 字典序 <= c_B，
    其中 c = (累计超标, 切换次数, 峰值出流, 峰值库容)（均为整数量子，比较精确）。

    无损性证明：从同一 (档位, 保持时长) 出发，若 v_A <= v_B，则对任意相同的
    后续档位序列，A 的逐时段库容与出流都不大于 B（水量平衡映射关于初始库容
    单调不减），故 A 的追加超标、追加峰值出流/库容均不更大，追加切换数完全
    相同。于是只要 c_A 字典序 <= c_B，对每一个后续序列都有
    "A 的总代价 字典序<= B 的总代价"（字典序首个不同分量的严格性会被单调
    追加量保持），即 B 能到达的任何解 A 都能不劣地到达 —— 删除 B 不丢最优。

    注意旧实现的两处有损性，此处均已修正：
      1. 旧规则只比较首目标（超标量 e），e 并列时无视切换次数/峰值，会误删
         字典序更优的状态；
      2. 前沿超过 120 点时的等距抽稀直接丢弃中间状态，近似解被当作最优返回。

    实现：按库容升序扫描，仅保留"代价元组创下字典序新低"的状态，O(k log k)。
    """
    items = sorted(bucket.items())          # (库容整数量子, (cost, parent))，v 升序
    kept = {}
    best = None
    for vk, entry in items:
        cost = entry[0]
        if best is not None and best <= cost:
            continue                        # 存在库容更低且代价字典序不更差的点
        kept[vk] = entry
        best = cost
    return kept


def _search(
    openings_step: list[int],
    inflows_step: list[int],
    capacity: int,
    v0: int,
    min_interval: int,
    qsafe_step: int,
    init_gear: int,
    init_held: int,
    max_states: Optional[int] = None,
    max_total_states: Optional[int] = None,
    max_raw_states: Optional[int] = None,
) -> SearchResult:
    """精确 DP 搜索（无损支配，绝不抽稀）。全部参数为整数量子，零浮点。

    found=False 表示全部状态被剪空（带切换约束下无解）。
    SearchLimitExceeded 表示单层/累计/原始状态数触限（规模拒绝，由调用方转述）。

    三个资源上限默认取模块级 DEFAULT_MAX_* 常量（运行时可修改常量以调整），
    也可显式传参覆盖（供测试）。

    层状态：buckets[gear][run] -> {时段末库容(整数量子): (代价元组, 父状态)}
      run ∈ 0..K：当前档位已连续保持的时段数，达到 K 即"可自由切换"
      （>=K 的状态等价，封顶存储，桶数因此封顶在 档位数 × (K+1)）。
    代价元组：(超标总量, 切换次数, 峰值出流, 峰值库容)，全为整数量子
    —— 不放入累计出流：它与库容严格等价（V=V0+ΣI−ΣR），
       放入会使支配剪枝失效（每个库容点代价都不同）。
    种子层为预报开始时闸门的现状档位 init_gear（已连续保持 init_held 个时段）。
    """
    if max_states is None:
        max_states = DEFAULT_MAX_STATES
    if max_total_states is None:
        max_total_states = DEFAULT_MAX_TOTAL_STATES
    if max_raw_states is None:
        max_raw_states = DEFAULT_MAX_RAW_STATES
    n = len(inflows_step)
    m = len(openings_step)
    K = min_interval
    K1 = K + 1
    # 各档位满开过流时的单时段超标水量（库内水量不足被夹小的情形另行现算）
    exceed_full = [o - qsafe_step if o > qsafe_step else 0
                   for o in openings_step]

    # 种子：第 0 时段之前，闸门处于现状档位（已保持时间在 K 处封顶：≥K 等价）
    seed_cost = (0, 0, 0, 0)
    prev = [[dict() for _ in range(K1)] for _ in range(m)]
    prev[init_gear][min(init_held, K)][v0] = (seed_cost, None)
    layers: list = []

    prune = {"overflow_pruned": 0, "overflow_by_step": {}, "first_empty_step": None,
             "max_layer_states": 1, "max_frontier_points": 1, "exact": True}
    total_states = 1

    for t in range(n):
        inflow = inflows_step[t]
        cur = [[dict() for _ in range(K1)] for _ in range(m)]
        overflow_t = 0
        raw_count = 0                 # 本层剪枝前原始状态数（瞬时内存保护）

        for gear in range(m):
            prow = prev[gear]
            o_stay = openings_step[gear]
            ex_stay = exceed_full[gear]
            cur_gear = cur[gear]
            for run_ in range(K1):
                vmap = prow[run_]
                if not vmap:
                    continue
                if run_ < K:
                    # 切换锁定：只能沿用当前档（保持时长 +1）
                    tgt = cur_gear[run_ + 1]
                    for vk, ((e0, s0, pr0, pv0), _par) in vmap.items():
                        avail = vk + inflow
                        if o_stay <= avail:
                            v_end = avail - o_stay
                            rel = o_stay
                            ex = ex_stay
                        else:                       # 库空夹档：放不出档位能力
                            v_end = 0
                            rel = avail
                            ex = avail - qsafe_step
                            if ex < 0:
                                ex = 0
                        if v_end > capacity:        # 精确容量判定：超 1 个量子即溢流
                            overflow_t += 1
                            continue
                        nc = (e0 + ex, s0,
                              pr0 if pr0 > rel else rel,
                              pv0 if pv0 > v_end else v_end)
                        old = tgt.get(v_end)        # 归并键 = 精确库容本身
                        if old is None or nc < old[0]:
                            if old is None:
                                raw_count += 1
                                if raw_count > max_raw_states:
                                    raise SearchLimitExceeded(max_raw_states, t)
                            tgt[v_end] = (nc, (gear, run_, vk))
                else:
                    # 已保持满 K：可沿用当前档，或切换到任意新档（重新计 1）
                    for vk, ((e0, s0, pr0, pv0), _par) in vmap.items():
                        avail = vk + inflow
                        s1 = s0 + 1
                        for j in range(m):
                            oj = openings_step[j]
                            if oj <= avail:
                                v_end = avail - oj
                                rel = oj
                                ex = exceed_full[j]
                            else:
                                v_end = 0
                                rel = avail
                                ex = avail - qsafe_step
                                if ex < 0:
                                    ex = 0
                            if v_end > capacity:
                                overflow_t += 1
                                continue
                            if j == gear:
                                nc = (e0 + ex, s0,
                                      pr0 if pr0 > rel else rel,
                                      pv0 if pv0 > v_end else v_end)
                                tgt = cur_gear[K]
                            else:
                                nc = (e0 + ex, s1,
                                      pr0 if pr0 > rel else rel,
                                      pv0 if pv0 > v_end else v_end)
                                tgt = cur[j][1]
                            old = tgt.get(v_end)
                            if old is None or nc < old[0]:
                                if old is None:
                                    raw_count += 1
                                    if raw_count > max_raw_states:
                                        raise SearchLimitExceeded(max_raw_states, t)
                                tgt[v_end] = (nc, (gear, run_, vk))

        if overflow_t:
            prune["overflow_pruned"] += overflow_t
            prune["overflow_by_step"][t] = overflow_t

        # 无损 Pareto 支配剪枝（同桶库容更低且代价字典序不更差者支配）
        state_count = 0
        max_frontier = 0
        for j in range(m):
            row = cur[j]
            for run_ in range(1, K1):     # run_=0 仅出现在种子层
                vmap = row[run_]
                if not vmap:
                    continue
                if len(vmap) > 1:
                    vmap = _dominance_filter(vmap)
                    row[run_] = vmap
                sz = len(vmap)
                state_count += sz
                if sz > max_frontier:
                    max_frontier = sz

        if state_count > prune["max_layer_states"]:
            prune["max_layer_states"] = state_count
        if max_frontier > prune["max_frontier_points"]:
            prune["max_frontier_points"] = max_frontier

        if state_count == 0:
            prune["first_empty_step"] = t
            return SearchResult(False, None, None, prune)
        total_states += state_count
        if state_count > max_states:
            raise SearchLimitExceeded(max_states, t)
        if total_states > max_total_states:
            raise SearchLimitExceeded(max_total_states, t)

        layers.append(cur)
        prev = cur

    # 末端选全局最优（完整代价元组字典序）
    best = None  # (cost, state_key)
    last = layers[-1]
    for j in range(m):
        for run_ in range(1, K1):
            for vk, (cost, _p) in last[j][run_].items():
                if best is None or cost < best[0]:
                    best = (cost, (j, run_, vk))

    # 回溯：被保留状态的父状态必然也被保留（支配关系沿转移封闭，
    # 见 _dominance_filter 的无损性证明），父链在 layers 中一定找得到
    chosen = [0] * n
    state = best[1]
    for t in range(n - 1, -1, -1):
        gear, run_, vk = state
        chosen[t] = gear
        _cost, parent = layers[t][gear][run_][vk]
        if parent is not None:
            state = parent

    return SearchResult(True, chosen, best[0], prune)


def _greedy_max(rate_step: int, inflows_step: list[int],
                capacity: int, v0: int) -> dict:
    """忽略切换约束、每时段均以最大可行开度泄放的贪心推演（整数量子，精确）。

    该轨迹逐时段库容不大于任何其他策略（早放水压库），
    因此若连它都溢流，则任何开度序列都无法避免溢流 —— 用于无解定性。
    """
    v = v0
    rows = []
    first_overflow = None
    deficit = 0
    for t, inflow in enumerate(inflows_step):
        avail = v + inflow
        release = rate_step if rate_step <= avail else avail
        v_end = avail - release
        overflow = v_end - capacity
        if overflow > 0:
            if first_overflow is None:
                first_overflow = t
                deficit = overflow
        else:
            overflow = 0
        rows.append((v, release, v_end, overflow))
        v = v_end
    return {"rows": rows, "first_overflow": first_overflow, "deficit": deficit}


def _min_safe_rate_step(inflows_step: list[int], capacity: int, v0: int) -> int:
    """"恒定最大开度"策略下恰好不溢流所需的最小单时段泄量（整数量子）。

    整数二分：ok() 关于泄量单调，hi 取 最大单时段入流+容量+1 必可行
    （泄量不低于任何可能的 库容+入流，每时段末库容都被压到 0）。
    """
    def ok(rate_step: int) -> bool:
        v = v0
        for inflow in inflows_step:
            avail = v + inflow
            v = avail - (rate_step if rate_step <= avail else avail)
            if v > capacity:
                return False
        return True

    lo = 0
    hi = (max(inflows_step) if inflows_step else 0) + capacity + 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return hi


def _search_limit_response(exc: "SearchLimitExceeded", echo: dict) -> dict:
    """规模拒绝（SEARCH_LIMIT）：精确求解的资源保护上限被触及时的可识别返回。

    求解器只返回严格最优解，绝不以抽稀前沿等方式给出近似；因此规模超限时
    明确拒绝，而不是把近似值标成最优。
    """
    return {
        "feasible": False,
        "params": echo,
        "infeasible": {
            "code": "SEARCH_LIMIT",
            "title": "规模超出精确求解的资源保护上限（已拒绝，未返回近似解）",
            "at_step": exc.step,
            "message": (
                f"推演到第 {exc.step + 1} 时段时，精确搜索的 DP 状态数"
                f"超过 {exc.limit:,} 的资源保护上限。本求解器只返回字典序"
                "严格最优的开度序列，拒绝用抽稀/截断前沿的方式产出近似解，"
                "故对该规模给出明确拒绝（这不是无解，也不是最优解的近似）。"
                "请缩小规模后重试。"
            ),
            "suggestions": [
                "减少预测时段数（或按洪峰分段推演）",
                "合并相近的闸门开度档位，降低档位数",
                "增大切换最小间隔，压缩 (档位 × 保持时长) 状态空间",
            ],
        },
    }


def solve(params: dict) -> dict:
    """执行完整推演。params 字段见 schemas.ScenarioInput。

    返回：
      feasible=True  -> steps/summary/objective/prune_stats
      feasible=False -> infeasible{code,title,at_step,message,suggestions,...}
    """
    capacity = float(params["capacity_m3"])
    v0 = float(params["initial_storage_m3"])
    dt_s = float(params["dt_s"])
    q_safe = float(params["q_safe"])
    min_interval = int(params["min_interval"])
    # 档位升序去重（Pydantic 层已保证非负/有限）
    openings = sorted({float(x) for x in params["openings"]})
    inflows = [float(x) for x in params["inflows"]]
    # 预报开始时闸门现状（1 基展示，内部转 0 基）；init_held=0 表示现状档
    # 刚切换到位、还须等满 min_interval 个时段后才能再次切换
    init_gear = int(params.get("initial_opening_index", 1)) - 1
    init_held = int(params.get("initial_held", 0))

    n = len(inflows)

    # 统一换算为精确整数量子：此后全部水量平衡/容量判定零舍入
    (scale, cap_q, v0_q, qsafe_step_q,
     openings_step_q, inflows_step_q, _extra_q) = _to_int_quantum(
        capacity, v0, dt_s, q_safe, openings, inflows)

    echo = {
        "capacity_m3": capacity,
        "initial_storage_m3": v0,
        "dt_s": dt_s,
        "q_safe": q_safe,
        "min_interval": min_interval,
        "openings": openings,
        "inflows": inflows,
        "initial_opening_index": init_gear + 1,
        "initial_held": init_held,
        "steps_count": n,
    }

    # ---------- 第零层诊断：预报开始时库容已超容（精确整数比较） ----------
    if v0_q > cap_q:
        return _already_overflow_block(v0_q, cap_q, scale, echo)

    # ---------- 第一层诊断：连"无视切换约束、满管泄放"都溢流 ----------
    greedy = _greedy_max(openings_step_q[-1], inflows_step_q, cap_q, v0_q)
    if greedy["first_overflow"] is not None:
        return _overflow_block(greedy, openings_step_q, inflows_step_q,
                               cap_q, v0_q, scale, dt_s, openings, echo)

    # ---------- 第二层：带切换最小间隔约束的精确搜索 ----------
    try:
        result = _search(openings_step_q, inflows_step_q, cap_q, v0_q,
                         min_interval, qsafe_step_q, init_gear, init_held)
    except SearchLimitExceeded as exc:
        return _search_limit_response(exc, echo)

    if not result.found:
        try:
            return _locked_block(result, openings_step_q, inflows_step_q,
                                 cap_q, v0_q, scale, min_interval,
                                 qsafe_step_q, init_gear, init_held, openings, echo)
        except SearchLimitExceeded as exc:
            return _search_limit_response(exc, echo)

    # ---------- 可行：组装"不溢流最优序列"的完整可行结果 ----------
    return _feasible_payload(
        result, cap_q, v0_q, qsafe_step_q, openings_step_q, inflows_step_q,
        scale, openings, inflows, dt_s, min_interval, init_gear, init_held, echo)


def _already_overflow_block(v0_q: int, cap_q: int, scale: int, echo: dict) -> dict:
    """第零层无解：预报开始时初始库容已超容（精确整数比较）。"""
    return {
        "feasible": False,
        "params": echo,
        "infeasible": {
            "code": "ALREADY_OVERFLOW",
            "title": "初始库容已超过调蓄池容量",
            "at_step": 0,
            "message": (
                f"预报开始（第 1 时段之前）库容为 {_qfmt(v0_q, scale)} m³，"
                f"已超过容量上限 {_qfmt(cap_q, scale)} m³，"
                f"超容 {_qfmt(v0_q - cap_q, scale)} m³。"
                "初始状态本身已处于溢流，任何开度序列都无法回溯消除；"
                "请先核实初始库容或扩大容量。"
            ),
            "suggestions": [
                f"核实/降低初始库容至 {_qfmt(cap_q, scale)} m³ 以下",
                f"将调蓄池容量扩大至少 {_qfmt(v0_q - cap_q, scale)} m³",
            ],
        },
    }


def _overflow_block(greedy: dict, openings_step_q, inflows_step_q,
                    cap_q, v0_q, scale, dt_s, openings, echo: dict) -> dict:
    """第一层无解：连"无视切换约束、满管泄放"都溢流（物理型无解）。"""
    k = greedy["first_overflow"]
    need_step_q = _min_safe_rate_step(inflows_step_q, cap_q, v0_q)
    need_rate = need_step_q / scale / dt_s          # m³/s（建议文案用）
    suggestions = [
        f"将闸门最大泄流能力由 {openings[-1]:g} m³/s 提高至"
        f"约 {need_rate:.6g} m³/s 以上",
        f"将调蓄池容量至少扩大约 {_qfmt(greedy['deficit'], scale)} m³"
        "（按首次溢流时刻缺口计）",
        "上游采取削峰/错峰措施，降低该时段入流预测",
    ]
    return {
        "feasible": False,
        "params": echo,
        "infeasible": {
            "code": "OVERFLOW_UNAVOIDABLE",
            "title": "库容必然溢流：泄流能力/库容不足",
            "at_step": k,
            "message": (
                f"第 {k + 1} 时段末，即使所有时段均以最大档位"
                f"（{openings[-1]:g} m³/s）泄放，库容仍达到 "
                f"{_qfmt(greedy['rows'][k][2], scale)} m³，"
                f"超过容量上限 {_qfmt(cap_q, scale)} m³，"
                f"缺口 {_qfmt(greedy['deficit'], scale)} m³。"
                "由于满管泄放得到的是逐时段最低可能库容轨迹，"
                "因此不存在任何能避免溢流的开度序列。"
            ),
            "suggestions": suggestions,
            "greedy_trajectory": [
                {
                    "t": i,
                    "storage_start_m3": r[0] / scale,
                    "release_m3": r[1] / scale,
                    "storage_end_m3": r[2] / scale,
                    "overflow_m3": r[3] / scale,
                }
                for i, r in enumerate(greedy["rows"])
            ],
        },
    }


def _locked_block(result: SearchResult, openings_step_q, inflows_step_q,
                  cap_q, v0_q, scale, min_interval, qsafe_step_q,
                  init_gear, init_held, openings, echo: dict) -> dict:
    """第三层无解：满管贪心不溢流，但现状档位 + 切换锁定导致被迫溢流。

    诊断子搜索（二分"现状档需再保持多久"）与主搜索同规模，同样可能触限 ——
    触限即原样抛出 SearchLimitExceeded，由调用方转述为规模拒绝。
    """
    already_free = init_held >= min_interval
    best_h = None
    lo, hi = 0, min_interval
    while lo <= hi:
        mid = (lo + hi) // 2
        if _search(openings_step_q, inflows_step_q, cap_q, v0_q,
                   min_interval, qsafe_step_q, init_gear, mid).found:
            best_h = mid
            hi = mid - 1
        else:
            lo = mid + 1

    # 用"锁定型贪心"见证策略给出被迫溢流的具体时段
    witness = _locked_greedy(openings_step_q, inflows_step_q, cap_q,
                             v0_q, min_interval, init_gear, init_held)
    at_step = witness["first_overflow"]
    prune_count = result.prune["overflow_pruned"]

    if already_free:
        cause = (
            "现状档位虽允许立即切换，但在当前切换最小间隔约束下，"
            "每次换到新档都须连续保持，搜索确认后续所有可达成的档位/库容状态"
            "都无法避免溢流。"
        )
        suggestions = [
            "减小切换最小间隔，使闸门可以更快逐级提升开度",
            "增加更高泄流能力的档位，使锁定期间也能跟上入流",
            "上游错峰，降低连续高入流时段的集中度",
        ]
    else:
        wait = (min_interval - init_held)
        cause = (
            f"预报开始时闸门处于 {init_gear + 1} 档"
            f"（{openings[init_gear]:g} m³/s）且该档只连续保持了 "
            f"{init_held} 个时段，按最小间隔 {min_interval} 的规则，"
            f"至少还须保持 {wait} 个时段才能切换；"
            "锁定期间提不起足够泄量导致库容被迫超容，"
            "等到允许切换时已来不及。"
        )
        h_sug = (f"允许现状档在保持满 {best_h} 个时段后即可切换"
                 if best_h is not None else "放宽切换最小间隔")
        suggestions = [
            h_sug + "（调度预案中预置应急切换权限）",
            f"将切换最小间隔由 {min_interval} 减小",
            "增加更高泄流能力的档位，或上游错峰削峰",
        ]

    return {
        "feasible": False,
        "params": echo,
        "infeasible": {
            "code": "LOCKED_INTERVAL",
            "title": "闸门切换锁定期间被迫溢流（约束型无解）",
            "at_step": at_step,
            "message": (
                f"约在第 {(at_step if at_step is not None else 0) + 1} 时段，"
                f"所有不溢流的开度组合都被切换规则排除。{cause}"
                "注：若无切换间隔限制（可随时满管泄放），方案不会溢流，"
                "因此瓶颈是调度约束而非库容或最大泄流能力。"
                f"（搜索中因溢流被剪枝的状态转移共 {prune_count} 次）"
            ),
            "suggestions": suggestions,
        },
        "prune_stats": result.prune,
    }


def _feasible_payload(result: SearchResult, cap_q, v0_q, qsafe_step_q,
                      openings_step_q, inflows_step_q, scale,
                      openings, inflows, dt_s, min_interval,
                      init_gear, init_held, echo: dict,
                      extra: Optional[dict] = None) -> dict:
    """按最优序列正向重建逐时段轨迹，组装完整的"可行"结果。

    extra（如容量反算的 plan 段）若非空，原样并入返回顶层。
    """
    steps, agg = _build_steps(chosen=result.chosen, openings=openings,
                              openings_step=openings_step_q, inflows=inflows,
                              inflows_step=inflows_step_q, capacity=cap_q,
                              v0=v0_q, qsafe_step=qsafe_step_q, scale=scale,
                              min_interval=min_interval, dt_s=dt_s,
                              init_gear=init_gear, init_held=init_held)

    max_storage_q = agg["max_storage"] if steps else v0_q
    if v0_q > max_storage_q:
        max_storage_q = v0_q
    max_storage = max_storage_q / scale
    capacity = cap_q / scale

    summary = {
        "total_inflow_m3": sum(inflows_step_q) / scale,
        "total_release_m3": agg["release_sum"] / scale,
        "final_storage_m3": steps[-1]["storage_end_m3"] if steps else echo["initial_storage_m3"],
        "max_storage_m3": max_storage,
        "max_storage_utilization": min(1.0, max_storage / capacity) if capacity > 0 else 0.0,
        "peak_release_m3s": agg["peak_release"] / scale / dt_s,
        "total_exceed_m3": agg["exceed_sum"] / scale,
        "exceed_steps": agg["exceed_steps"],
        "switches": result.cost[1],
        "overflow_m3": 0.0,
    }

    payload = {
        "feasible": True,
        "params": echo,
        "steps": steps,
        "summary": summary,
        "objective": {
            "total_exceed_m3": result.cost[0] / scale,
            "switches": result.cost[1],
            "peak_release_m3": result.cost[2] / scale,
            "peak_storage_m3": result.cost[3] / scale,
            "rule": "先保证不溢流，再最小化超标总量；并列时切换次数/峰值出流/峰值库容更小者优",
        },
        "prune_stats": result.prune,
    }
    if extra:
        payload.update(extra)
    return payload


# ============================================================================
# 容量反算（size-capacity）：给定超标预算 B 与设计容量上界 U，在接口输入的
# "共同十进制量子" q=10^-D m³ 网格上，求满足  全程不溢流 且 超标总量 ≤ B
# 的最小设计库容 C*；在 C* 上按 solve 的既有目标选出见证序列。
#
# 可行性谓词（对容量 C 精确判定）：
#     F(C) := 存在一条满足切换锁定约束的档位序列，使全程不溢流 且
#             该序列的最优超标总量 e*(C) ≤ B
# 其中 e*(C) 由既有精确 DP（_search，目标①即超标总量）给出 —— 容量判定与
# 超标累计全部在同一套整数量子上完成，故 F 是**精确**的（无容差、无舍入）。
#
# 单调性：F(C) 关于 C 单调不减。证明：水量平衡映射 V_end = max(0, V+I−o·Δt)
# 对容量参数本身无依赖，容量只在"V_end ≤ C"这一步做可行性截断；C 增大只会
# 放开状态（更小容量下存活的每条轨迹在更大容量下同样存活，其超标量不变），
# 故 e*(C) 单调不增、可达序列集合单调扩大。因此 C* 是良定义的单一阈值，
# 可在整数网格 [v0, U] 上做二分；任一试算 SEARCH_LIMIT 都意味着该次 F(C)
# 的真值**未知**，单调性区间不能继续收缩 —— 整个请求必须规模拒绝。
#
# 容量候选网格：D 取 初始库容 / 容量上界 / 超标预算 / 各单时段水量 的最大
# 小数位，v0、U、B 与 C* 都是 q 的整数倍（输入是有限十进制，换算零舍入）。
# ============================================================================

def _plan_echo(params: dict, n: int, cap_hi: float) -> dict:
    """容量反算请求的参数回显（不含具体设计容量；见证结果里另填 C*）。"""
    return {
        "initial_storage_m3": float(params["initial_storage_m3"]),
        "dt_s": float(params["dt_s"]),
        "q_safe": float(params["q_safe"]),
        "min_interval": int(params["min_interval"]),
        "openings": sorted({float(x) for x in params["openings"]}),
        "inflows": [float(x) for x in params["inflows"]],
        "initial_opening_index": int(params.get("initial_opening_index", 1)),
        "initial_held": int(params.get("initial_held", 0)),
        "steps_count": n,
        "allowed_exceed_m3": float(params["allowed_exceed_m3"]),
        "capacity_upper_m3": cap_hi,
    }


def _plan_meta(*, cap_hi_q, v0_q, budget_q, scale, grid_min_q,
               trials, infeasible_low_q, feasible_high_q, found_star,
               star_q=None, upper_infeasible: bool = False) -> dict:
    """组装容量反算的 plan 段（搜索区间、量子、试算日志）。"""
    def m3(qv):
        return None if qv is None else qv / scale
    note = None
    if upper_infeasible:
        # 上界 U 已被精确证明不可行；可行性谓词关于容量单调，故整个候选网格
        # [V0, U] 上的每个容量都不可行，已证不可行侧的右端点就是 U。
        note = (
            "设计容量上界处已被精确证明不可行；可行性关于容量单调，"
            "故候选网格内不大于该上界的容量全部不可行（不可行下界即上界本身）。")
    elif infeasible_low_q is None and found_star:
        # 唯一 null 情形：答案 C*=网格最小点（初始库容=0），其下一个量子为 −q，
        # 不是非负容量，无法把"不可行下界"表示成容量值。
        note = (
            "最小设计库容恰为网格最小点（初始库容）；其再小一个量子即为负容量，"
            "不存在可表示的非负候选，故不可行下界以 null 表示。")
    return {
        "allowed_exceed_m3": budget_q / scale,
        "capacity_upper_m3": cap_hi_q / scale,
        "quantum_m3": 1 / scale,
        "grid_minimum_m3": grid_min_q / scale,
        "minimum_capacity_m3": m3(star_q) if found_star else None,
        "infeasible_below_m3": m3(infeasible_low_q),
        "feasible_at_or_above_m3": m3(feasible_high_q),
        "infeasible_low_note": note,
        "trials_count": len(trials),
        "trials": trials,
    }


def size_capacity(params: dict) -> dict:
    """容量反算主入口。params 字段见 schemas.CapacityPlanInput。

    返回：
      可行        -> feasible=True，steps/summary/objective 为 C* 上的见证序列，
                     plan 段含最小容量、已证不可行下界/可行上界、量子与试算日志；
      上界不可行  -> feasible=False，infeasible.code ∈
                     {OVERFLOW_UNAVOIDABLE, LOCKED_INTERVAL, BUDGET_INFEASIBLE}，
                     plan 段含已证实的搜索区间；
      规模拒绝    -> feasible=False，infeasible.code="SEARCH_LIMIT"：
                     任一试算触限，整个请求拒绝（未完成试算绝不当作不可行）。
    """
    v0 = float(params["initial_storage_m3"])
    dt_s = float(params["dt_s"])
    q_safe = float(params["q_safe"])
    min_interval = int(params["min_interval"])
    openings = sorted({float(x) for x in params["openings"]})
    inflows = [float(x) for x in params["inflows"]]
    init_gear = int(params.get("initial_opening_index", 1)) - 1
    init_held = int(params.get("initial_held", 0))
    allowed = float(params["allowed_exceed_m3"])
    cap_hi = float(params["capacity_upper_m3"])
    n = len(inflows)

    # 共同十进制量子：容量上界与超标预算也参与定标，保证二者恰为整数个量子
    (scale, cap_hi_q, v0_q, qsafe_step_q,
     openings_step_q, inflows_step_q, extra_q) = _to_int_quantum(
        cap_hi, v0, dt_s, q_safe, openings, inflows,
        extra_m3=[allowed, v0])
    budget_q = extra_q[0]
    # extra_q[1] == v0_q（v0 参与定标后仍是精确整数，无需回取）

    echo = _plan_echo({**params, "openings": openings, "inflows": inflows}, n, cap_hi)
    grid_min_q = v0_q                      # 设计容量至少要装得下初始库容

    # ---- 第零层：初始库容已高于设计上界（物理不可能，无需试算） ----
    if v0_q > cap_hi_q:
        echo["capacity_m3"] = cap_hi
        block = _already_overflow_block(v0_q, cap_hi_q, scale, echo)
        block["plan"] = _plan_meta(
            cap_hi_q=cap_hi_q, v0_q=v0_q, budget_q=budget_q, scale=scale,
            grid_min_q=grid_min_q, trials=[],
            infeasible_low_q=None, feasible_high_q=None, found_star=False)
        return block

    trials: list = []

    def run_trial(cap_q: int) -> SearchResult:
        """在给定候选容量上做一次精确试算；触限原样上抛（调用方必须规模拒绝）。"""
        return _search(openings_step_q, inflows_step_q, cap_q, v0_q,
                       min_interval, qsafe_step_q, init_gear, init_held)

    def log_trial(cap_q: int, kind: str, result: Optional[SearchResult],
                  lo_q, hi_q):
        entry = {
            "capacity_m3": cap_q / scale,
            "verdict": kind,                  # feasible / overflow / locked / rejected(不应出现)
            "bracket_infeasible_below_m3": (lo_q / scale if lo_q is not None and lo_q >= 0 else None),
            "bracket_feasible_at_or_above_m3": (hi_q / scale if hi_q is not None else None),
        }
        if result is not None:
            entry["min_exceed_m3"] = (result.cost[0] / scale
                                      if result.found else None)
        trials.append(entry)

    # ---- 上界试算：U 可行是继续二分的前提（其触限必须整体规模拒绝） ----
    try:
        hi_res = run_trial(cap_hi_q)
    except SearchLimitExceeded as exc:
        echo["capacity_m3"] = cap_hi
        resp = _search_limit_response(exc, echo)
        resp["plan"] = _plan_meta(
            cap_hi_q=cap_hi_q, v0_q=v0_q, budget_q=budget_q, scale=scale,
            grid_min_q=grid_min_q, trials=trials,
            infeasible_low_q=None, feasible_high_q=None, found_star=False)
        return resp

    # 分析型已知不可行上界（无需再试算，直接给原因）
    if not hi_res.found:
        echo["capacity_m3"] = cap_hi
        # 先判物理型（满管也溢流）还是约束型（切换锁定）
        greedy = _greedy_max(openings_step_q[-1], inflows_step_q,
                             cap_hi_q, v0_q)
        if greedy["first_overflow"] is not None:
            block = _overflow_block(greedy, openings_step_q, inflows_step_q,
                                    cap_hi_q, v0_q, scale, dt_s, openings, echo)
        else:
            try:
                block = _locked_block(hi_res, openings_step_q, inflows_step_q,
                                      cap_hi_q, v0_q, scale, min_interval,
                                      qsafe_step_q, init_gear, init_held,
                                      openings, echo)
            except SearchLimitExceeded as exc:
                resp = _search_limit_response(exc, echo)
                resp["plan"] = _plan_meta(
                    cap_hi_q=cap_hi_q, v0_q=v0_q, budget_q=budget_q, scale=scale,
                    grid_min_q=grid_min_q, trials=trials,
                    infeasible_low_q=None, feasible_high_q=None, found_star=False)
                return resp
        log_trial(cap_hi_q,
                  "overflow" if greedy["first_overflow"] is not None else "locked",
                  hi_res, cap_hi_q, None)
        block["feasible"] = False
        block["plan"] = _plan_meta(
            cap_hi_q=cap_hi_q, v0_q=v0_q, budget_q=budget_q, scale=scale,
            grid_min_q=grid_min_q, trials=trials,
            infeasible_low_q=cap_hi_q, feasible_high_q=None, found_star=False,
            upper_infeasible=True)
        return block

    # 上界处存在不溢流序列，但超标预算可能仍达不到
    hi_exceed_q = hi_res.cost[0]
    if hi_exceed_q > budget_q:
        echo["capacity_m3"] = cap_hi
        witness_echo = dict(echo)
        witness = _feasible_payload(
            hi_res, cap_hi_q, v0_q, qsafe_step_q, openings_step_q,
            inflows_step_q, scale, openings, inflows, dt_s, min_interval,
            init_gear, init_held, witness_echo)
        log_trial(cap_hi_q, "budget", hi_res, cap_hi_q, None)
        block = {
            "feasible": False,
            "params": echo,
            "infeasible": {
                "code": "BUDGET_INFEASIBLE",
                "title": "设计容量上界处仍无法满足超标预算（预算型不可行）",
                "at_step": None,
                "message": (
                    f"即使把设计库容取到允许的上界 {_qfmt(cap_hi_q, scale)} m³，"
                    f"在切换最小间隔 {min_interval} 的约束下，全程不溢流序列能达到的"
                    f"最小下游超标总量仍为 {_qfmt(hi_exceed_q, scale)} m³，"
                    f"高于允许的超标预算 {_qfmt(budget_q, scale)} m³"
                    f"（还差 {_qfmt(hi_exceed_q - budget_q, scale)} m³）。"
                    "更大库容只能消除溢流，不能进一步压低超标：超标来自闸门档位"
                    "的过流能力本身（高档位泄量超过下游安全流量），故继续扩容"
                    "也无法满足该预算。"
                ),
                "suggestions": [
                    f"将允许的下游超标总量由 {_qfmt(budget_q, scale)} m³ "
                    f"放宽到至少 {_qfmt(hi_exceed_q, scale)} m³",
                    "增加更贴近下游安全流量的闸门档位（低过流能力档），"
                    "使压库期间不必长时间高泄",
                    "上游削峰/错峰，降低需要大泄量赶水的时段集中度",
                ],
            },
            "witness": witness,
        }
        block["plan"] = _plan_meta(
            cap_hi_q=cap_hi_q, v0_q=v0_q, budget_q=budget_q, scale=scale,
            grid_min_q=grid_min_q, trials=trials,
            infeasible_low_q=cap_hi_q, feasible_high_q=None, found_star=False,
            upper_infeasible=True)
        return block

    # ---- 二分求最小可行容量（整数网格；单调性见模块段注释） ----
    # lo_q：已证不可行侧（初值 v0−1 是解析下界：容量装不下初始库容，
    #        无需试算；为负时区间展示用 null）；hi_q：已证可行侧。
    lo_q = grid_min_q - 1
    hi_q = cap_hi_q
    star_res: Optional[SearchResult] = hi_res
    log_trial(cap_hi_q, "feasible", hi_res, lo_q, hi_q)

    while hi_q - lo_q > 1:
        mid_q = (lo_q + hi_q) // 2
        try:
            res = run_trial(mid_q)
        except SearchLimitExceeded as exc:
            # 该试算真值未知：绝不能把它当成不可行而收缩 lo。整个请求规模拒绝，
            # 并带回此前已严格证实的区间（不含本次未完成试算）。
            echo["capacity_m3"] = cap_hi
            resp = _search_limit_response(exc, echo)
            resp["plan"] = _plan_meta(
                cap_hi_q=cap_hi_q, v0_q=v0_q, budget_q=budget_q, scale=scale,
                grid_min_q=grid_min_q, trials=trials,
                infeasible_low_q=(lo_q if lo_q >= 0 else None),
                feasible_high_q=hi_q, found_star=False)
            return resp
        ok = res.found and res.cost[0] <= budget_q
        if ok:
            hi_q = mid_q
            star_res = res
            log_trial(mid_q, "feasible", res, lo_q, hi_q)
        else:
            lo_q = mid_q
            # 不可行原因仅供日志：溢流/锁定（found=False）或超预算（found=True）
            kind = ("budget" if res.found else "infeasible")
            log_trial(mid_q, kind, res, lo_q, hi_q)

    star_q = hi_q
    proven_low_q = lo_q if lo_q >= 0 else None

    # ---- 在 C* 上按既有目标（超标→切换→峰值出流→峰值库容）选见证序列 ----
    echo["capacity_m3"] = star_q / scale
    plan = _plan_meta(
        cap_hi_q=cap_hi_q, v0_q=v0_q, budget_q=budget_q, scale=scale,
        grid_min_q=grid_min_q, trials=trials,
        infeasible_low_q=proven_low_q, feasible_high_q=star_q,
        found_star=True, star_q=star_q)
    payload = _feasible_payload(
        star_res, star_q, v0_q, qsafe_step_q, openings_step_q,
        inflows_step_q, scale, openings, inflows, dt_s, min_interval,
        init_gear, init_held, echo, extra={"plan": plan})
    # 见证序列的超标总量恰为 e*(C*)，且 ≤ 预算（二分可行性已精确保证）
    payload["plan"]["witness_exceed_within_budget"] = (
        star_res.cost[0] <= budget_q)
    return payload



def _locked_greedy(openings_step, inflows_step, capacity, v0,
                   min_interval, gear, run) -> dict:
    """见证策略：遵守切换锁定（从现状档位出发），允许切换时选最高可行档。

    用于在 LOCKED_INTERVAL 无解时给出"被迫溢流发生在第几时段"的直观见证。
    全部整数量子，溢流判定精确。
    """
    v = v0
    first_overflow = None
    rows = []
    for t, inflow in enumerate(inflows_step):
        if run < min_interval:
            choices = (gear,)
        else:
            choices = range(len(openings_step))
        # 在可选档位中选使时段末库容最小的（最高可行档）
        best_j, best_v = None, None
        for j in choices:
            avail = v + inflow
            ve = avail - (openings_step[j] if openings_step[j] <= avail else avail)
            if best_v is None or ve < best_v:
                best_j, best_v = j, ve
        j = best_j
        avail = v + inflow
        release = openings_step[j] if openings_step[j] <= avail else avail
        v_end = avail - release
        if first_overflow is None and v_end > capacity:
            first_overflow = t
        run = 1 if gear != j else run + 1
        gear = j
        rows.append({"t": t, "gear": j, "storage_end_q": v_end})
        v = v_end
    return {"first_overflow": first_overflow, "rows": rows}


def _build_steps(*, chosen, openings, openings_step, inflows, inflows_step,
                 capacity, v0, qsafe_step, scale, min_interval, dt_s,
                 init_gear, init_held):
    """按最优档位序列正向重建逐时段结果（整数量子），并标注触发的约束/事件。

    返回 (steps, agg)：steps 为展示用浮点字段（由整数量子一次性换算，
    时段间库容严格衔接）；agg 为汇总所需的整数累计量。
    """
    steps = []
    agg = {"release_sum": 0, "exceed_sum": 0, "max_storage": 0,
           "peak_release": 0, "exceed_steps": 0}
    cap_f = capacity / scale
    full_margin = scale // 1000      # 1e-3 m³ 对应的量子数（不足 1 量子时仅恰打满才标 FULL）
    qsafe_rate = qsafe_step / scale / dt_s
    v = v0
    prev_gear = init_gear
    run_before = init_held  # 现状档位已连续保持的时段数（进入 t=0 之前）

    for t, j in enumerate(chosen):
        locked = run_before < min_interval
        switched = j != prev_gear

        oj = openings_step[j]
        avail = v + inflows_step[t]
        release = oj if oj <= avail else avail      # 库空时不能放出没有的水
        v_end = avail - release
        exceed = release - qsafe_step
        if exceed < 0:
            exceed = 0

        v_f = v / scale
        v_end_f = v_end / scale
        rel_f = release / scale
        utilization = v_end_f / cap_f if cap_f > 0 else 0.0

        events = []
        if locked:
            events.append({
                "level": "warn",
                "code": "LOCKED",
                "text": f"切换锁定：现档位需连续保持 {min_interval} 个时段"
                        f"（已保持 {run_before}），不能换档",
            })
        if switched:
            events.append({
                "level": "info",
                "code": "SWITCH",
                "text": f"档位切换：{prev_gear + 1} 档 → {j + 1} 档"
                        f"（{openings[prev_gear]:g} → {openings[j]:g} m³/s）",
            })
        if exceed > 0:
            events.append({
                "level": "danger",
                "code": "EXCEED",
                "text": f"下游超标：出流 {rel_f / dt_s:g} m³/s 超过安全上限 "
                        f"{qsafe_rate:g} m³/s，本时段超标水量 "
                        f"{_qfmt(exceed, scale)} m³",
            })
        if v_end >= capacity - full_margin:
            events.append({
                "level": "danger",
                "code": "FULL",
                "text": "库容打满，处于溢洪临界（已选取该状态下最大可行泄放）",
            })
        elif utilization >= 0.9:
            events.append({
                "level": "warn",
                "code": "HIGH",
                "text": f"高水位运行，库容利用率 {utilization * 100:.0f}%",
            })
        if release < oj:
            events.append({
                "level": "info",
                "code": "EMPTY",
                "text": "库内可供水量不足，实际出流低于档位过流能力",
            })
        if not events:
            events.append({
                "level": "ok",
                "code": "NORMAL",
                "text": "运行正常，约束均满足",
            })

        steps.append({
            "t": t,
            "inflow_m3s": inflows[t],
            "inflow_m3": inflows_step[t] / scale,
            "opening_index": j,
            "opening_label": f"{j + 1} 档",
            "opening_m3s": openings[j],
            "release_m3": rel_f,
            "release_m3s": rel_f / dt_s,
            "storage_start_m3": v_f,
            "storage_end_m3": v_end_f,
            "utilization": utilization,
            "exceed_m3": exceed / scale,
            "locked": locked,
            "events": events,
        })

        agg["release_sum"] += release
        agg["exceed_sum"] += exceed
        if v_end > agg["max_storage"]:
            agg["max_storage"] = v_end
        if release > agg["peak_release"]:
            agg["peak_release"] = release
        if exceed > 0:
            agg["exceed_steps"] += 1

        run_before = 1 if switched else run_before + 1
        prev_gear = j
        v = v_end

    return steps, agg
