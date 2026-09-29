"""FastAPI 入口：场景 CRUD、推演计算、历史结果；原生页面静态托管。"""
from __future__ import annotations

import os
from typing import List

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from . import database, engine

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="城市雨洪调蓄池泄放序列推演台", version="1.0")


# ---------------- Schemas ----------------

class _ScenarioBase(BaseModel):
    """推演/容量反算共用的方案输入（不含设计容量；容量由各子模型补充）。"""
    name: str = Field(min_length=1, max_length=80, description="方案名称")
    initial_storage_m3: float = Field(ge=0, description="初始库容 (m³)")
    dt_s: float = Field(gt=0, description="时间步长 (秒)")
    q_safe: float = Field(ge=0, description="下游安全流量上限 (m³/s)")
    min_interval: int = Field(ge=1, le=200, description="闸门切换最小间隔 (个时段)")
    initial_opening_index: int = Field(
        default=1, ge=1, description="预报开始时闸门所处档位（1 基）")
    initial_held: int = Field(
        default=0, ge=0, description="现状档位此前已连续保持的时段数；"
        "0=刚切换到位（须等满最小间隔），≥最小间隔=可立即切换")
    openings: List[float] = Field(min_length=1, max_length=30,
                                  description="闸门开度档位过流能力 (m³/s)，升序")
    inflows: List[float] = Field(min_length=1, max_length=200,
                                 description="分时段入流预测 (m³/s)")

    @field_validator("openings")
    @classmethod
    def _check_openings(cls, v: List[float]) -> List[float]:
        if any(x < 0 for x in v):
            raise ValueError("闸门开度不能为负")
        return v

    @field_validator("inflows")
    @classmethod
    def _check_inflows(cls, v: List[float]) -> List[float]:
        if any(x < 0 for x in v):
            raise ValueError("入流不能为负")
        return v

    @model_validator(mode="after")
    def _check_gear_range(self):
        if not (1 <= self.initial_opening_index <= len(self.openings)):
            raise ValueError("初始档位序号超出闸门档位数")
        return self

    def normalized(self) -> dict:
        data = self.model_dump()
        data["openings"] = sorted(set(round(x, 6) for x in data["openings"]))
        data["inflows"] = [round(x, 6) for x in data["inflows"]]
        return data


class ScenarioInput(_ScenarioBase):
    capacity_m3: float = Field(gt=0, description="调蓄池容量 (m³)")

    @model_validator(mode="after")
    def _check_consistency(self):
        if self.initial_storage_m3 > self.capacity_m3:
            raise ValueError("初始库容不能超过调蓄池容量")
        return self


class ScenarioCreate(ScenarioInput):
    pass


class ScenarioUpdate(ScenarioInput):
    pass


class CapacityPlanInput(_ScenarioBase):
    """容量反算输入：沿用入流/档位/间隔/初始状态，另填超标预算与容量上界。"""
    allowed_exceed_m3: float = Field(
        ge=0, description="允许的下游安全流量超标总量 (m³)，反算的超标预算")
    capacity_upper_m3: float = Field(
        gt=0, description="设计容量上界 (m³)，反算二分的最大候选")

    @model_validator(mode="after")
    def _check_plan_consistency(self):
        if self.initial_storage_m3 > self.capacity_upper_m3:
            raise ValueError("初始库容不能超过设计容量上界")
        return self


# ---------------- API ----------------

@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/solve")
def solve_adhoc(payload: ScenarioInput):
    """不入库，直接按当前表单参数推演（"立即推演"按钮使用）。"""
    return engine.solve(payload.normalized())


@app.post("/api/size-capacity")
def size_capacity_api(payload: CapacityPlanInput):
    """容量反算：在输入共同十进制量子网格上求满足超标预算且全程不溢流的
    最小设计库容，并返回 C* 上的见证序列与已证实的搜索区间（不入库）。"""
    return engine.size_capacity(payload.normalized())


@app.get("/api/scenarios")
def get_scenarios():
    return database.list_scenarios()


@app.post("/api/scenarios")
def create_scenario_api(payload: ScenarioCreate):
    params = payload.normalized()
    sid = database.create_scenario(params["name"], params)
    return {"id": sid}


@app.get("/api/scenarios/{sid}")
def get_scenario_api(sid: int):
    scenario = database.get_scenario(sid)
    if not scenario:
        raise HTTPException(404, "场景不存在")
    scenario["runs"] = database.list_runs(sid)
    return scenario


@app.put("/api/scenarios/{sid}")
def update_scenario_api(sid: int, payload: ScenarioUpdate):
    if not database.get_scenario(sid):
        raise HTTPException(404, "场景不存在")
    params = payload.normalized()
    database.update_scenario(sid, params["name"], params)
    return {"ok": True}


@app.delete("/api/scenarios/{sid}")
def delete_scenario_api(sid: int):
    database.delete_scenario(sid)
    return {"ok": True}


@app.post("/api/scenarios/{sid}/run")
def run_scenario_api(sid: int):
    """对已保存场景执行一次推演并留存结果。"""
    scenario = database.get_scenario(sid)
    if not scenario:
        raise HTTPException(404, "场景不存在")
    result = engine.solve(scenario["params"])
    run_id = database.add_run(sid, result)
    result["run_id"] = run_id
    result["scenario_id"] = sid
    return result


@app.get("/api/scenarios/{sid}/runs")
def list_runs_api(sid: int):
    if not database.get_scenario(sid):
        raise HTTPException(404, "场景不存在")
    return database.list_runs(sid)


@app.get("/api/runs/{run_id}")
def get_run_api(run_id: int):
    result = database.get_run(run_id)
    if not result:
        raise HTTPException(404, "推演记录不存在")
    return result


# ---------------- 静态页面 ----------------

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def _startup():
    database.init_db()
