/* 城市雨洪调蓄池泄放序列推演台 —— 前端逻辑（原生 JS） */
"use strict";

const $ = (id) => document.getElementById(id);

const DEMO = {
  name: "12 小时设计暴雨（60min 步长，初始关闸 1 档）",
  capacity_m3: 80000,
  initial_storage_m3: 2000,
  dt_min: 60,
  q_safe: 12,
  min_interval: 2,
  initial_opening_index: 1,
  initial_held: 0,
  openings: [0, 3, 6, 9, 12, 16],
  inflows: [2, 5, 12, 20, 18, 12, 7, 4, 2, 1, 0, 0],
};

let currentScenarioId = null;
let currentResult = null;

/* ---------------- 数字/单位工具 ---------------- */

const fmtInt = (x) => (x == null || isNaN(x) ? "—" : Math.round(x).toLocaleString("zh-CN"));
const fmt1 = (x) => (x == null || isNaN(x) ? "—" : x.toLocaleString("zh-CN", { maximumFractionDigits: 1 }));
const fmt2 = (x) => (x == null || isNaN(x) ? "—" : x.toLocaleString("zh-CN", { maximumFractionDigits: 2 }));
const fmtTime = (ts) => new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });

/* ---------------- 表单 ---------------- */

function renderGearChips(values) {
  const box = $("gears");
  box.innerHTML = "";
  values.forEach((v, i) => {
    const chip = document.createElement("div");
    chip.className = "chip";
    chip.innerHTML = `<span class="tag">${i + 1}档</span>`;
    const inp = document.createElement("input");
    inp.type = "number"; inp.min = "0"; inp.step = "0.1"; inp.value = v;
    chip.appendChild(inp);
    const del = document.createElement("button");
    del.type = "button"; del.textContent = "✕"; del.title = "删除该档";
    del.onclick = () => {
      const cur = readOpenings();
      cur.splice(i, 1);
      if (cur.length) {
        renderGearChips(cur);
        const ig = $("f-init-gear");
        if (parseInt(ig.value, 10) > cur.length) ig.value = cur.length;
      }
    };
    chip.appendChild(del);
    box.appendChild(chip);
  });
}

function renderInflowCells(values) {
  const box = $("inflows");
  box.innerHTML = "";
  values.forEach((v, i) => {
    const cell = document.createElement("div");
    cell.className = "cell";
    cell.innerHTML = `<span class="idx">t=${i + 1}</span>`;
    const inp = document.createElement("input");
    inp.type = "number"; inp.min = "0"; inp.step = "0.1"; inp.value = v;
    cell.appendChild(inp);
    box.appendChild(cell);
  });
}

function readList(selector) {
  return [...document.querySelectorAll(selector)].map((e) => parseFloat(e.value));
}
function readOpenings() { return readList("#gears input"); }
function readInflows() { return readList("#inflows input"); }

function loadForm(p, name) {
  $("f-name").value = name ?? p.name ?? "未命名方案";
  $("f-dt-min").value = (p.dt_s ?? p.dt_min * 60) / 60;
  $("f-capacity").value = p.capacity_m3;
  $("f-v0").value = p.initial_storage_m3;
  $("f-qsafe").value = p.q_safe;
  $("f-min-int").value = p.min_interval;
  $("f-init-gear").value = p.initial_opening_index ?? 1;
  $("f-init-held").value = p.initial_held ?? 0;
  renderGearChips(p.openings);
  renderInflowCells(p.inflows);
  // 容量规划默认值：预算 0、上界优先用请求中带回的值，否则取当前容量 ×1.5（用户可改）
  $("f-allowed-exceed").value = p.allowed_exceed_m3 ?? 0;
  $("f-cap-upper").value = p.capacity_upper_m3 != null
    ? p.capacity_upper_m3
    : Math.round((p.capacity_m3 || 80000) * 1.5);
}

function collectBaseParams() {
  const err = $("form-error");
  err.hidden = true;
  const fail = (msg) => { err.textContent = msg; err.hidden = false; return null; };

  const name = $("f-name").value.trim() || "未命名方案";
  const dtMin = parseFloat($("f-dt-min").value);
  const v0 = parseFloat($("f-v0").value);
  const qsafe = parseFloat($("f-qsafe").value);
  const minInt = parseInt($("f-min-int").value, 10);
  const initGear = parseInt($("f-init-gear").value, 10);
  const initHeld = parseInt($("f-init-held").value, 10);
  const openings = readOpenings();
  const inflows = readInflows();

  if (!(dtMin > 0)) return fail("时间步长必须为正数（分钟）。");
  if (isNaN(v0) || v0 < 0) return fail("初始库容必须 ≥ 0。");
  if (isNaN(qsafe) || qsafe < 0) return fail("下游安全流量上限必须 ≥ 0。");
  if (!(minInt >= 1)) return fail("切换最小间隔必须 ≥ 1 个时段。");
  if (!(initGear >= 1)) return fail("初始档位序号必须 ≥ 1。");
  if (isNaN(initHeld) || initHeld < 0) return fail("现状档已保持时段数必须 ≥ 0。");
  if (openings.some((x) => isNaN(x) || x < 0)) return fail("闸门开度必须为非负数。");
  if (openings.length === 0) return fail("至少保留 1 个闸门档位。");
  if (initGear > openings.length) return fail(`初始档位超出档位数（当前共 ${openings.length} 档）。`);
  if (inflows.some((x) => isNaN(x) || x < 0)) return fail("各时段入流必须为非负数。");
  if (inflows.length === 0) return fail("至少录入 1 个时段的入流。");

  return {
    name,
    initial_storage_m3: v0,
    dt_s: dtMin * 60,
    q_safe: qsafe,
    min_interval: minInt,
    initial_opening_index: initGear,
    initial_held: initHeld,
    openings,
    inflows,
  };
}

function collectParams() {
  const base = collectBaseParams();
  if (!base) return null;
  const err = $("form-error");
  const fail = (msg) => { err.textContent = msg; err.hidden = false; return null; };
  const capacity = parseFloat($("f-capacity").value);
  if (!(capacity > 0)) return fail("调蓄池容量必须为正数。");
  if (base.initial_storage_m3 > capacity) return fail("初始库容不能超过调蓄池容量。");
  return { ...base, capacity_m3: capacity };
}

function collectPlanParams() {
  const base = collectBaseParams();
  if (!base) return null;
  const err = $("form-error");
  const fail = (msg) => { err.textContent = msg; err.hidden = false; return null; };
  const allowed = parseFloat($("f-allowed-exceed").value);
  const capUpper = parseFloat($("f-cap-upper").value);
  if (isNaN(allowed) || allowed < 0) return fail("允许的下游超标总量必须 ≥ 0。");
  if (!(capUpper > 0)) return fail("设计容量上界必须为正数。");
  if (base.initial_storage_m3 > capUpper) return fail("初始库容不能超过设计容量上界。");
  return { ...base, allowed_exceed_m3: allowed, capacity_upper_m3: capUpper };
}

/* ---------------- 场景列表 ---------------- */

async function refreshScenarios(activeId) {
  const list = await api("GET", "/api/scenarios");
  const ul = $("scenario-list");
  ul.innerHTML = "";
  if (!list.length) {
    ul.innerHTML = `<li style="color:#93a2b1;border-style:dashed;background:transparent">暂无保存场景</li>`;
    return;
  }
  for (const s of list) {
    const li = document.createElement("li");
    const dot = s.last_feasible == null ? "none" : (s.last_feasible ? "ok" : "bad");
    li.innerHTML = `<span class="dot ${dot}"></span>`;
    const nm = document.createElement("span");
    nm.className = "name";
    nm.textContent = s.name;
    nm.title = "载入参数";
    nm.onclick = () => openScenario(s.id);
    li.appendChild(nm);
    if (s.last_run_at) {
      const tm = document.createElement("span");
      tm.className = "time";
      tm.textContent = new Date(s.last_run_at * 1000).toLocaleDateString("zh-CN");
      li.appendChild(tm);
    }
    const del = document.createElement("button");
    del.className = "del"; del.textContent = "🗑"; del.title = "删除场景";
    del.onclick = async (e) => {
      e.stopPropagation();
      if (confirm(`确定删除场景「${s.name}」及其推演历史？`)) {
        await api("DELETE", `/api/scenarios/${s.id}`);
        if (currentScenarioId === s.id) clearActive();
        refreshScenarios();
      }
    };
    li.appendChild(del);
    ul.appendChild(li);

    const runs = await api("GET", `/api/scenarios/${s.id}/runs`);
    if (runs.length) {
      const hist = document.createElement("ul");
      hist.className = "run-history";
      const shown = runs.slice(0, 5);
      shown.forEach((r) => {
        const b = document.createElement("button");
        b.textContent = `${r.feasible ? "✓ 可行" : "✕ 无解"} · ${new Date(r.created_at * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}`;
        b.onclick = () => loadRun(r.id);
        const hi = document.createElement("li");
        hi.appendChild(b);
        hist.appendChild(hi);
      });
      ul.appendChild(hist);
    }
  }
}

function clearActive() {
  currentScenarioId = null;
  $("btn-update").hidden = true;
}

async function openScenario(id) {
  const s = await api("GET", `/api/scenarios/${id}`);
  currentScenarioId = id;
  loadForm(s.params, s.name);
  $("btn-update").hidden = false;
  $("form-error").hidden = true;
}

async function loadRun(runId) {
  const r = await api("GET", `/api/runs/${runId}`);
  currentScenarioId = r.scenario_id;
  if (r.params) loadForm(r.params, r.params.name);
  $("btn-update").hidden = false;
  renderResult(r);
}

/* ---------------- 动作 ---------------- */

async function runSolve(persist) {
  const params = collectParams();
  if (!params) return;
  const btn = $("btn-solve");
  btn.disabled = true; btn.textContent = "推演中…";
  try {
    let result;
    if (persist) {
      if (!currentScenarioId) {
        currentScenarioId = await api("POST", "/api/scenarios", params).then((r) => r.id);
        $("btn-update").hidden = false;
      } else {
        await api("PUT", `/api/scenarios/${currentScenarioId}`, params);
      }
      result = await api("POST", `/api/scenarios/${currentScenarioId}/run`);
      refreshScenarios(currentScenarioId);
    } else {
      result = await api("POST", "/api/solve", params);
    }
    renderResult(result);
  } catch (e) {
    const err = $("form-error");
    err.textContent = e.message;
    err.hidden = false;
  } finally {
    btn.disabled = false; btn.textContent = "▶ 立即推演";
  }
}

async function runSize() {
  const params = collectPlanParams();
  if (!params) return;
  const btn = $("btn-size");
  btn.disabled = true; btn.textContent = "容量反算中…（多次精确试算）";
  try {
    const result = await api("POST", "/api/size-capacity", params);
    renderResult(result);
  } catch (e) {
    const err = $("form-error");
    err.textContent = e.message;
    err.hidden = false;
  } finally {
    btn.disabled = false; btn.textContent = "📏 容量反算最小库容";
  }
}

async function api(method, url, body) {
  const resp = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) {
    let msg = `请求失败 (${resp.status})`;
    try {
      const d = await resp.json();
      msg = d.detail ? JSON.stringify(d.detail) : msg;
    } catch (_) { /* ignore */ }
    throw new Error(msg);
  }
  return resp.json();
}

/* ---------------- 结果渲染 ---------------- */

/* 前端安全网：后端返回 feasible=true 时，先按原始参数复核容量与水量平衡，
 * 任何超容/不守恒的结果都不得被页面标注为"精确可行"。 */
function consistencyError(r) {
  const p = r.params || {};
  const cap = p.capacity_m3;
  if (!r.steps || !r.summary) return "结果缺少逐时段明细";
  let v = p.initial_storage_m3;
  const tol = 1e-9 * Math.max(1, cap); // 仅用于浮点显示值复核，超容判定为零容差
  for (const s of r.steps) {
    if (s.storage_end_m3 > cap) {
      return `第 ${s.t + 1} 时段末库容 ${s.storage_end_m3} m³ 超过容量上限 ${cap} m³`;
    }
    if (Math.abs(s.storage_start_m3 - v) > tol) return `第 ${s.t + 1} 时段初库容与上一时段末不衔接`;
    const bal = s.storage_start_m3 + s.inflow_m3 - s.release_m3;
    if (Math.abs(s.storage_end_m3 - bal) > tol) return `第 ${s.t + 1} 时段水量平衡不一致`;
    v = s.storage_end_m3;
  }
  if (r.summary.max_storage_m3 > cap) return "库容峰值超过容量上限";
  return null;
}

function renderResult(r) {
  currentResult = r;
  $("placeholder").hidden = true;
  $("result").hidden = false;

  // 容量反算：不可行但带见证（BUDGET_INFEASIBLE）时，图表/明细展示上界处见证，
  // 横幅与原因仍按"不可行"渲染。
  const witness = (!r.feasible && r.witness) ? r.witness : null;
  const display = witness || r;

  const verdict = $("verdict");
  const p = display.params || {};
  // 预算型不可行时展示的是"上界处见证序列"，同样须通过容量/水量平衡复核
  const witnessBad = witness ? consistencyError(witness) : null;
  if (r.feasible) {
    const s = r.summary;
    const bad = consistencyError(r);    if (bad) {
      verdict.innerHTML = `
      <div class="verdict bad">
        <span class="v-icon">✕</span>
        <div>
          <h2>结果未通过容量/水量平衡一致性校验，不作为精确可行解展示</h2>
          <p>${escapeHtml(bad)}。该结果不可采用，请将其作为异常上报。</p>
        </div>
      </div>`;
    } else {
      const isPlan = !!r.plan;
      verdict.innerHTML = `
      <div class="verdict ok">
        <span class="v-icon">✓</span>
        <div>
          <h2>${isPlan
            ? `容量反算完成：最小设计库容 ${fmtCap(r.plan.minimum_capacity_m3)} m³`
            : "推演可行：已求得不溢流的精确最优闸门开度序列"}</h2>
          <p>共 ${p.steps_count} 个时段，闸门切换 ${s.switches} 次；
          库容峰值 ${fmtInt(s.max_storage_m3)} m³（利用率 ${(s.max_storage_utilization * 100).toFixed(1)}%），
          下游超标 ${s.exceed_steps} 个时段、超标总量 <b>${fmt1(s.total_exceed_m3)} m³</b>。<br>
          <span style="color:var(--ok)">（库容状态精确整数表示、零舍入；无损 Pareto 前沿完整保留，未做抽稀，为字典序严格最优解）</span></p>
        </div>
      </div>`;
    }
  } else {
    const inf = r.infeasible;
    const rejected = inf.code === "SEARCH_LIMIT";
    if (witnessBad) {
      verdict.innerHTML = `
      <div class="verdict bad">
        <span class="v-icon">✕</span>
        <div>
          <h2>见证序列未通过容量/水量平衡一致性校验，不予展示</h2>
          <p>${escapeHtml(witnessBad)}。该结果不可采用，请将其作为异常上报；原始不可行原因：
          <code>${escapeHtml(inf.code)}</code>。</p>
        </div>
      </div>`;
    } else {
    const title = rejected
      ? "规模拒绝：未返回任何结果（既非无解，也非近似最优）"
      : inf.title;
    verdict.innerHTML = `
      <div class="verdict ${rejected ? "warn" : "bad"}">
        <span class="v-icon">${rejected ? "⛔" : "✕"}</span>
        <div>
          <h2>${title}</h2>
          <p>类型：<code>${inf.code}</code>${inf.at_step != null ? `　首次问题时段：第 ${inf.at_step + 1} 时段` : ""}</p>
        </div>
      </div>`;
    }
  }

  renderPlanCard(r);
  renderChips(display);
  if ((display.feasible || display.steps) && !witnessBad) {
    // BUDGET_INFEASIBLE：见证图表以见证容量（设计上界）画容量线
    renderCharts(display);
    renderTable(display);
    if (witness) {
      renderInfeasible(r);
      $("charts").insertAdjacentHTML(
        "afterbegin",
        `<div class="chart-title witness-note">下列库容/出流过程与明细为「设计容量上界 ${fmtCap(p.capacity_m3)} m³ 处的见证序列」（最小可达超标 ${fmt1(witness.summary.total_exceed_m3)} m³，仍高于预算，故该上界不满足预算）。</div>`);
    } else {
      $("infeasible").innerHTML = "";
    }
  } else {
    $("charts").innerHTML = "";
    $("table-wrap").innerHTML = "";
    renderInfeasible(r);
  }
  $("engine-detail").hidden = false;
  $("engine-stats").textContent = JSON.stringify(
    r.feasible
      ? { objective: r.objective, prune_stats: r.prune_stats,
          ...(r.plan ? { plan: planStats(r.plan) } : {}) }
      : { prune_stats: r.prune_stats || "（见无解说明中的剪枝次数）",
          ...(r.plan ? { plan: planStats(r.plan) } : {}) },
    null, 2
  );
}

function kpi(label, value, unit, cls) {
  return `<div class="kpi ${cls || ""}"><div class="k-label">${label}</div>
    <div class="k-value">${value} <span class="k-unit">${unit || ""}</span></div></div>`;
}

function renderChips(r) {
  if (!r.feasible) {
    const p = r.params;
    $("chips").innerHTML =
      kpi("总时段数", p.steps_count, "个") +
      kpi("库容上限", fmtInt(p.capacity_m3), "m³") +
      kpi("最大泄流能力", fmt2(p.openings[p.openings.length - 1]), "m³/s") +
      kpi("切换最小间隔", p.min_interval, "时段", "danger");
    return;
  }
  const s = r.summary;
  $("chips").innerHTML =
    kpi("超标总量（目标）", fmt1(s.total_exceed_m3), "m³", s.total_exceed_m3 > 0 ? "danger" : "ok") +
    kpi("库容峰值", fmtInt(s.max_storage_m3), `m³ / ${(s.max_storage_utilization * 100).toFixed(0)}%`,
        s.max_storage_utilization >= 0.95 ? "danger" : s.max_storage_utilization >= 0.8 ? "warn" : "") +
    kpi("出流峰值", fmt2(s.peak_release_m3s), "m³/s") +
    kpi("闸门切换", s.switches, "次") +
    kpi("超标的时段", s.exceed_steps, "个", s.exceed_steps ? "warn" : "ok") +
    kpi("末库容", fmtInt(s.final_storage_m3), "m³");
}

function renderInfeasible(r) {
  const inf = r.infeasible;
  const rejected = inf.code === "SEARCH_LIMIT";
  const sug = inf.suggestions.map((s) => `<li>${escapeHtml(s)}</li>`).join("");
  let extra = "";
  if (inf.code === "OVERFLOW_UNAVOIDABLE" && inf.greedy_trajectory) {
    const g = inf.greedy_trajectory;
    extra = `<div class="chart-block"><div class="chart-title">满管泄放下仍溢流的库容轨迹（任意其他序列库容只会更高）</div>
      ${storageSvg(g.map((x) => ({ storage_end_m3: x.storage_end_m3, overflow_m3: x.overflow_m3 })),
                   r.params.capacity_m3, r.params.initial_storage_m3)}</div>`;
  }
  $("infeasible").innerHTML = `
    <div class="infeasible-card${rejected ? " rejected" : ""}">
      <h3>${rejected ? "规模拒绝原因（未给出最优解，也未给出近似解）" : "无解原因"}</h3>
      ${inf.at_step != null ? `<span class="at">定位：第 ${inf.at_step + 1} 时段</span>` : ""}
      <p class="msg">${escapeHtml(inf.message)}</p>
      <h4>${rejected ? "如何让规模落入精确求解范围" : "可行化建议"}</h4>
      <ol>${sug}</ol>
    </div>${extra}`;
}

/* ---------------- 容量规划（容量反算）卡片 ---------------- */

/* 容量可能落在 1e-6 m³ 量子上：整数用千分位，小数保留必要精度（最多 6 位）。 */
function fmtCap(x) {
  if (x == null || isNaN(x)) return "—";
  const s = x.toLocaleString("en-US", { maximumFractionDigits: 6, minimumFractionDigits: 0 });
  return s;
}

function planStats(pl) {
  return {
    minimum_capacity_m3: pl.minimum_capacity_m3,
    infeasible_below_m3: pl.infeasible_below_m3,
    feasible_at_or_above_m3: pl.feasible_at_or_above_m3,
    quantum_m3: pl.quantum_m3,
    grid_minimum_m3: pl.grid_minimum_m3,
    allowed_exceed_m3: pl.allowed_exceed_m3,
    capacity_upper_m3: pl.capacity_upper_m3,
    trials_count: pl.trials_count,
  };
}

function renderPlanCard(r) {
  const box = $("plan-card");
  const pl = r.plan;
  if (!pl) { box.innerHTML = ""; return; }
  const rejected = !r.feasible && r.infeasible && r.infeasible.code === "SEARCH_LIMIT";
  const found = pl.minimum_capacity_m3 != null;

  const head = found
    ? `<h3>容量反算结果：最小设计库容 <b>${fmtCap(pl.minimum_capacity_m3)}</b> m³
        <span class="plan-tag ok">已证实最小</span></h3>
       <p class="plan-sub">超标预算 ${fmtCap(pl.allowed_exceed_m3)} m³、设计容量上界
        ${fmtCap(pl.capacity_upper_m3)} m³ 下，全程不溢流的最小可取容量。
        下方见证序列即按该容量推演所得（沿用 超标总量→切换次数→峰值出流→峰值库容 的既有目标）。</p>`
    : `<h3>容量反算${rejected ? "中止" : "未求得最小库容"}
        <span class="plan-tag ${rejected ? "warn" : "bad"}">${rejected ? "规模拒绝" : "不可行"}</span></h3>
       <p class="plan-sub">搜索区间中已被精确证实的部分仍列于下方；${rejected
          ? "某次试算触发资源保护上限、真值未知，未完成的试算没有被当作不可行，区间不再继续收缩。"
          : "设计容量上界处即已不可行，原因见上方横幅与建议。"}</p>`;

  const kpis = [
    ["最小设计库容", found ? fmtCap(pl.minimum_capacity_m3) : "—", "m³", found ? "ok" : ""],
    ["已证不可行下界", pl.infeasible_below_m3 == null ? "—" : fmtCap(pl.infeasible_below_m3),
     "m³（该容量及以下不可行）", "danger"],
    ["已证可行上界", pl.feasible_at_or_above_m3 == null ? "—" : fmtCap(pl.feasible_at_or_above_m3),
     "m³（该容量起可行）", "ok"],
    ["容量量子", fmtCap(pl.quantum_m3), "m³/格", ""],
  ].map(([l, v, u, c]) => kpi(l, v, u, c)).join("");

  const bar = planBracketBar(pl, found);
  const log = planTrialTable(pl);
  const note = pl.infeasible_low_note
    ? `<p class="plan-note">注：${escapeHtml(pl.infeasible_low_note)}</p>` : "";

  box.innerHTML = `
    <div class="plan-card">
      ${head}
      <div class="chips">${kpis}</div>
      ${bar}
      ${log}
      ${note}
    </div>`;
}

function planBracketBar(pl, found) {
  const lo = pl.grid_minimum_m3;
  const hi = pl.capacity_upper_m3;
  const span = Math.max(hi - lo, 0);
  const pct = (v) => (span > 0 ? Math.min(100, Math.max(0, ((v - lo) / span) * 100)) : 0);
  const cStar = pl.minimum_capacity_m3;
  const provenLo = pl.infeasible_below_m3;
  const provenHi = pl.feasible_at_or_above_m3;

  // 已证不可行段：[网格最小点, provenLo]；已证可行段起点：provenHi
  let segs = "";
  if (provenLo != null && provenLo >= lo) {
    segs += `<div class="seg seg-bad" style="left:0%;width:${pct(provenLo)}%"></div>`;
  }
  if (provenHi != null && provenHi <= hi) {
    segs += `<div class="seg seg-ok" style="left:${pct(provenHi)}%;width:${100 - pct(provenHi)}%"></div>`;
  }
  const marks = [];
  const mark = (v, cls, label) =>
    `<div class="bmark ${cls}" style="left:${pct(v)}%"><span>${label}</span></div>`;
  marks.push(mark(lo, "m-lo", `网格最小点 ${fmtCap(lo)}`));
  if (provenLo != null) marks.push(mark(provenLo, "m-bad", `不可行 ≤ ${fmtCap(provenLo)}`));
  if (found && cStar != null) marks.push(mark(cStar, "m-star", `C* = ${fmtCap(cStar)}`));
  if (provenHi != null && (!found || provenHi !== cStar))
    marks.push(mark(provenHi, "m-ok", `可行 ≥ ${fmtCap(provenHi)}`));
  marks.push(mark(hi, "m-hi", `上界 ${fmtCap(hi)}`));

  return `<div class="bracket-block">
    <div class="chart-title">搜索区间（容量轴，单位 m³）</div>
    <div class="bracket-bar">${segs}${marks.join("")}</div>
    <div class="bracket-legend">
      <span><i class="lg-bad"></i>已精确证实不可行</span>
      <span><i class="lg-ok"></i>已精确证实可行</span>
      <span><i class="lg-unknown"></i>未判定</span>
    </div>
  </div>`;
}

function planTrialTable(pl) {
  if (!pl.trials || !pl.trials.length) {
    return `<div class="chart-title" style="margin-top:10px">试算日志：无完成试算</div>`;
  }
  const rows = pl.trials.map((t, i) => {
    const ok = t.verdict === "feasible";
    const cls = ok ? "ok" : (t.verdict === "budget" ? "warn" : "bad");
    const txt = { feasible: "可行", budget: "超预算", infeasible: "溢流/锁定",
                  overflow: "满管仍溢流", locked: "切换锁定无解" }[t.verdict] || t.verdict;
    return `<tr>
      <td class="t">${i + 1}</td>
      <td>${fmtCap(t.capacity_m3)}</td>
      <td><span class="tv tv-${cls}">${txt}</span></td>
      <td>${t.min_exceed_m3 == null ? "—" : fmtCap(t.min_exceed_m3)}</td>
      <td>${t.bracket_infeasible_below_m3 == null ? "—" : fmtCap(t.bracket_infeasible_below_m3)}</td>
      <td>${t.bracket_feasible_at_or_above_m3 == null ? "—" : fmtCap(t.bracket_feasible_at_or_above_m3)}</td>
    </tr>`;
  }).join("");
  return `<details class="plan-trials" open>
    <summary>试算日志（共 ${pl.trials_count} 次精确试算）</summary>
    <div class="table-wrap">
      <table class="steps plan-log">
        <thead><tr>
          <th class="t">#</th><th>试算容量 (m³)</th><th>判定</th>
          <th>最小超标 (m³)</th><th>试算后不可行下界 (m³)</th><th>试算后可行上界 (m³)</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  </details>`;
}

function renderTable(r) {
  const rows = r.steps;
  const dtH = r.params.dt_s / 3600;
  const trs = rows.map((s) => {
    const utilCls = s.utilization >= 0.999 ? "full" : s.utilization >= 0.9 ? "high" : "";
    const evs = s.events.map((e) => `<span class="ev ev-${e.level}">${escapeHtml(e.text)}</span>`).join("");
    const rowCls = [s.exceed_m3 > 0 ? "exceed-row" : "", s.locked ? "locked-row" : ""].join(" ");
    return `<tr class="${rowCls}">
      <td class="t">${s.t + 1}</td>
      <td>${fmt2(s.inflow_m3s)}</td>
      <td>${s.opening_label} · ${fmt2(s.opening_m3s)}</td>
      <td>${fmt2(s.release_m3s)}</td>
      <td>${fmtInt(s.storage_start_m3)}</td>
      <td>
        <span class="util-bar"><i class="${utilCls}" style="width:${Math.min(100, s.utilization * 100)}%"></i></span>
        ${(s.utilization * 100).toFixed(1)}%
      </td>
      <td>${fmtInt(s.storage_end_m3)}</td>
      <td>${s.exceed_m3 > 1e-6 ? fmt1(s.exceed_m3) : "0"}</td>
      <td><div class="events">${evs}</div></td>
    </tr>`;
  }).join("");
  $("table-wrap").innerHTML = `
    <div class="chart-block" style="margin-bottom:8px">
      <div class="chart-title">逐时段推演明细（步长 ${dtH} h；浅黄底=切换锁定时段，红字=下游超标）</div>
    </div>
    <div class="table-wrap">
      <table class="steps">
        <thead><tr>
          <th class="t">时段</th><th>入流<br>(m³/s)</th><th>选定开度</th>
          <th>实际出流<br>(m³/s)</th><th>时段初库容<br>(m³)</th>
          <th>库容<br>利用率</th><th>时段末库容<br>(m³)</th>
          <th>超标水量<br>(m³)</th><th>触发约束 / 事件</th>
        </tr></thead>
        <tbody>${trs}</tbody>
      </table>
    </div>`;
}

/* ---------------- SVG 图表 ---------------- */

function renderCharts(r) {
  const st = r.steps;
  $("charts").innerHTML = `
    <div class="chart-block">
      <div class="chart-title">库容过程</div>
      <div class="legend">
        <span><i style="background:var(--storage)"></i>时段末库容 (m³)</span>
        <span><i style="background:var(--danger)"></i>容量上限 ${fmtInt(r.params.capacity_m3)} m³</span>
        <span><i style="background:var(--warn)"></i>高水位线 90%</span>
      </div>
      ${storageSvg(st, r.params.capacity_m3, r.params.initial_storage_m3)}
    </div>
    <div class="chart-block">
      <div class="chart-title">入流 / 出流过程</div>
      <div class="legend">
        <span><i style="background:var(--inflow)"></i>入流 (m³/s)</span>
        <span><i style="background:var(--release)"></i>实际出流 (m³/s)</span>
        <span><i style="background:var(--safe)"></i>下游安全上限 ${fmt2(r.params.q_safe)} m³/s</span>
        <span><i style="background:repeating-linear-gradient(45deg,#f3c8c3,#f3c8c3 4px,#fff 4px,#fff 8px)"></i>超标区段</span>
      </div>
      ${flowSvg(st, r.params.q_safe)}
    </div>`;
}

function svgFrame(n, maxY, yLabel) {
  const W = 920, H = 230, L = 58, R = 18, T = 16, B = 34;
  const pw = W - L - R, ph = H - T - B;
  const x = (i) => L + (n <= 1 ? pw / 2 : (i / (n - 1)) * pw);
  const y = (v) => T + ph - (maxY > 0 ? (v / maxY) * ph : 0);
  let g = "";
  const ticks = 4;
  for (let k = 0; k <= ticks; k++) {
    const v = (maxY * k) / ticks;
    const yy = y(v);
    g += `<line x1="${L}" y1="${yy}" x2="${W - R}" y2="${yy}" stroke="var(--grid)" stroke-width="1"/>`;
    g += `<text x="${L - 6}" y="${yy + 3.5}" text-anchor="end" font-size="10" fill="#8a98a6">${Math.round(v).toLocaleString("zh-CN")}</text>`;
  }
  for (let i = 0; i < n; i++) {
    const xx = x(i);
    g += `<text x="${xx}" y="${H - 12}" text-anchor="middle" font-size="10" fill="#8a98a6">${i + 1}</text>`;
    g += `<line x1="${xx}" y1="${H - B + 2}" x2="${xx}" y2="${H - B - 2}" stroke="#c3ced9" stroke-width="1"/>`;
  }
  g += `<text x="14" y="${T + 10}" font-size="10" fill="#8a98a6">${yLabel}</text>`;
  g += `<line x1="${L}" y1="${T}" x2="${L}" y2="${H - B}" stroke="#b6c2cd"/>`;
  g += `<line x1="${L}" y1="${H - B}" x2="${W - R}" y2="${H - B}" stroke="#b6c2cd"/>`;
  return { W, H, L, R, T, B, x, y, g };
}

function storageSvg(steps, capacity, v0) {
  const n = steps.length;
  const vals = [v0, ...steps.map((s) => s.storage_end_m3), capacity * 1.1];
  const maxY = Math.max(...vals) * 1.08;
  const f = svgFrame(n, maxY, "m³");
  const x = (i) => f.x(i);
  const pts = steps.map((s, i) => `${x(i)},${f.y(s.storage_end_m3)}`).join(" ");
  const areaPts = n > 1
    ? `${x(0)},${f.y(steps[0].storage_end_m3)} ${pts} ${x(n - 1)},${f.y(0)} ${x(0)},${f.y(0)}`
    : "";
  let g = f.g;
  g += `<line x1="${f.L}" y1="${f.y(capacity)}" x2="${f.W - f.R}" y2="${f.y(capacity)}" stroke="var(--danger)" stroke-width="1.6" stroke-dasharray="7 4"/>`;
  g += `<line x1="${f.L}" y1="${f.y(capacity * 0.9)}" x2="${f.W - f.R}" y2="${f.y(capacity * 0.9)}" stroke="var(--warn)" stroke-width="1" stroke-dasharray="3 4"/>`;
  if (n > 1 && areaPts) g += `<polygon points="${areaPts}" fill="var(--storage-fill)" opacity="0.55"/>`;
  g += `<polyline points="${pts}" fill="none" stroke="var(--storage)" stroke-width="2.2"${n === 1 ? "" : ""}/>`;
  steps.forEach((s, i) => {
    const overflow = (s.overflow_m3 || 0) > 1e-6 || s.storage_end_m3 > capacity + 1e-6;
    g += `<circle cx="${x(i)}" cy="${f.y(s.storage_end_m3)}" r="${overflow ? 4.5 : 3.4}"
          fill="${overflow ? "var(--danger)" : "var(--storage)"}" stroke="#fff" stroke-width="1.2">
          <title>t=${i + 1}：${fmtInt(s.storage_end_m3)} m³</title></circle>`;
  });
  return `<svg class="chart" viewBox="0 0 ${f.W} ${f.H}" preserveAspectRatio="none">${g}</svg>`;
}

function flowSvg(steps, qsafe) {
  const n = steps.length;
  const maxY = Math.max(qsafe, ...steps.map((s) => s.inflow_m3s), ...steps.map((s) => s.release_m3s), 1) * 1.15;
  const f = svgFrame(n, maxY, "m³/s");
  const x = (i) => f.x(i);
  let g = f.g;

  // 超标区段（出流高于安全线的时间区间阴影）
  for (let i = 0; i < n; i++) {
    if (steps[i].exceed_m3 > 1e-6) {
      const xa = x(i) - (n > 1 ? (f.x(1) - f.x(0)) / 2 : 14);
      const xb = x(i) + (n > 1 ? (f.x(1) - f.x(0)) / 2 : 14);
      g += `<rect x="${xa}" y="${f.T}" width="${xb - xa}" height="${f.H - f.T - f.B}"
            fill="var(--danger)" opacity="0.07"/>`;
    }
  }

  g += `<line x1="${f.L}" y1="${f.y(qsafe)}" x2="${f.W - f.R}" y2="${f.y(qsafe)}"
        stroke="var(--safe)" stroke-width="1.6" stroke-dasharray="7 4"/>`;

  const line = (key, color, width) => {
    const pts = steps.map((s, i) => `${x(i)},${f.y(s[key])}`).join(" ");
    let el = `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="${width}"/>`;
    steps.forEach((s, i) => {
      el += `<circle cx="${x(i)}" cy="${f.y(s[key])}" r="2.8" fill="${color}" stroke="#fff" stroke-width="0.8">
             <title>t=${i + 1}：${fmt2(s[key])} m³/s</title></circle>`;
    });
    return el;
  };
  g += line("inflow_m3s", "var(--inflow)", 1.6);
  g += line("release_m3s", "var(--release)", 2.4);
  return `<svg class="chart" viewBox="0 0 ${f.W} ${f.H}" preserveAspectRatio="none">${g}</svg>`;
}

function escapeHtml(t) {
  return String(t).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

/* ---------------- 初始化 ---------------- */

$("btn-add-gear").onclick = () => {
  const cur = readOpenings();
  cur.push(cur.length ? Math.round((cur[cur.length - 1] + 3) * 10) / 10 : 0);
  renderGearChips(cur);
};
$("btn-add-inflow").onclick = () => {
  const cur = readInflows();
  cur.push(0);
  renderInflowCells(cur);
};
$("btn-del-inflow").onclick = () => {
  const cur = readInflows();
  if (cur.length > 1) { cur.pop(); renderInflowCells(cur); }
};
$("btn-solve").onclick = () => runSolve(false);
$("btn-size").onclick = () => runSize();
$("btn-save").onclick = async () => {
  const params = collectParams();
  if (!params) return;
  if (currentScenarioId) {
    await api("PUT", `/api/scenarios/${currentScenarioId}`, params);
  } else {
    currentScenarioId = (await api("POST", "/api/scenarios", params)).id;
    $("btn-update").hidden = false;
  }
  refreshScenarios(currentScenarioId);
  const err = $("form-error");
  err.textContent = "场景已保存。";
  err.className = "form-error";
  err.style.background = "var(--ok-soft)";
  err.style.color = "var(--ok)";
  err.hidden = false;
  setTimeout(() => {
    err.hidden = true;
    err.style.background = ""; err.style.color = "";
  }, 2000);
};
$("btn-update").onclick = () => runSolve(true);
$("btn-reset").onclick = () => {
  clearActive();
  loadForm(DEMO);
};

loadForm(DEMO);
refreshScenarios();
