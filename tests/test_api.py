"""端到端 API 验证。

优先使用 fastapi.testclient.TestClient；若环境未安装 fastapi/httpx，
则回退到文件内置的、仅依赖标准库的最小 ASGI 调用器（直接构造 ASGI scope
驱动 app，不经网络），覆盖同样的断言语义。

运行：
   pip install -r requirements.txt
   PYTHONPATH=. BASIN_DB_PATH=/tmp/e2e_basin.db python3 -m pytest tests/ -q
   # 或：
   PYTHONPATH=. BASIN_DB_PATH=/tmp/e2e_basin.db python3 tests/test_api.py
"""
import json
import os
import sys
import tempfile
import typing as _typing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_db = tempfile.mktemp(suffix=".db")
os.environ["BASIN_DB_PATH"] = _db
if os.path.exists(_db):
    os.remove(_db)

try:
    from fastapi.testclient import TestClient
    from app.main import app  # noqa: E402
    client = TestClient(app)
    BACKEND = "fastapi.testclient"
except Exception:  # pragma: no cover - 无 fastapi/httpx 环境下的 stdlib 回退
    # 用仅依赖标准库的最小桩件提供 pydantic/fastapi 的子集语义（校验 + 路由 +
    # ASGI），使 tests 能在未 pip install 的环境中端到端驱动 app.main。
    import types

    # ---- 最小 pydantic 子集 ----
    pyd = types.ModuleType("pydantic")

    def Field(default=None, **kw):
        return (default, kw)

    def field_validator(*names):
        def deco(fn):
            fn._validator_fields = names
            fn._validator_kind = "field"
            return fn
        return deco

    def model_validator(*a, **k):
        def deco(fn):
            fn._validator_kind = "model"
            fn._validator_mode = k.get("mode", "after")
            return fn
        return deco

    class ValidationError(Exception):
        def __init__(self, errors):
            super().__init__(str(errors))
            self.errors_list = errors

    _PY_TYPES = {int: int, float: (int, float), str: str, bool: bool}
    _MISSING = object()

    class BaseModel:
        def __init__(self, **data):
            cls = type(self)
            ann = {}
            for k in cls.__mro__:
                ann.update(getattr(k, "__annotations__", {}))
            errs = []
            values = {}
            for name, typ in ann.items():
                fval = cls.__dict__.get(name, _MISSING)
                if isinstance(fval, tuple) and len(fval) == 2 and isinstance(fval[1], dict):
                    default, meta = fval
                elif fval is not _MISSING:
                    default, meta = fval, {}
                else:
                    default, meta = None, {}
                if name in data:
                    v = data[name]
                elif meta.get("default_factory"):
                    v = meta["default_factory"]()
                elif default is not _MISSING:
                    v = default
                else:
                    errs.append({"loc": (name,), "msg": "field required"}); continue
                if v is None:
                    errs.append({"loc": (name,), "msg": "none not allowed"}); continue
                # 泛型 List[X]
                args = getattr(typ, "__args__", None)
                is_list = args is not None and (
                    getattr(typ, "__origin__", None) in (list, _typing.List)
                    or str(getattr(typ, "__origin__", None)) == "list")
                if is_list:
                    inner = args[0] if args else object
                    if not isinstance(v, list):
                        errs.append({"loc": (name,), "msg": "not a list"}); continue
                    try:
                        v = [_coerce(inner, x) for x in v]
                    except Exception:
                        errs.append({"loc": (name,), "msg": "bad item"}); continue
                else:
                    try:
                        v = _coerce(typ, v)
                    except Exception:
                        errs.append({"loc": (name,), "msg": f"not {typ}"}); continue
                # 约束
                if "gt" in meta and not v > meta["gt"]:
                    errs.append({"loc": (name,), "msg": "gt failed"})
                if "ge" in meta and not v >= meta["ge"]:
                    errs.append({"loc": (name,), "msg": "ge failed"})
                if "le" in meta and not v <= meta["le"]:
                    errs.append({"loc": (name,), "msg": "le failed"})
                if "min_length" in meta and len(v) < meta["min_length"]:
                    errs.append({"loc": (name,), "msg": "min_length"})
                if "max_length" in meta and len(v) > meta["max_length"]:
                    errs.append({"loc": (name,), "msg": "max_length"})
                values[name] = v
            # field validators
            for name in dir(cls):
                fn = getattr(cls, name, None)
                if callable(fn) and getattr(fn, "_validator_kind", None) == "field":
                    for fld in fn._validator_fields:
                        if fld in values:
                            try:
                                values[fld] = fn(cls, values[fld])
                            except ValueError as e:
                                errs.append({"loc": (fld,), "msg": str(e)})
            if errs:
                raise ValidationError(errs)
            self.__dict__.update(values)
            # model validators
            for name in dir(cls):
                fn = getattr(cls, name, None)
                if callable(fn) and getattr(fn, "_validator_kind", None) == "model":
                    try:
                        fn(self)
                    except ValueError as e:
                        raise ValidationError([{"loc": (), "msg": str(e)}])

        def model_dump(self):
            return dict(self.__dict__)

    def _coerce(typ, v):
        if typ is float and isinstance(v, int):
            return float(v)
        if typ in (int, float, str):
            if not isinstance(v, _PY_TYPES[typ] if typ in _PY_TYPES else typ):
                # bool is int
                if typ is int and isinstance(v, bool):
                    return int(v)
                raise TypeError(typ)
            return typ(v) if typ is not str else v
        return v

    # typing.List 支持
    import typing as _typing
    List = _typing.List

    pyd.BaseModel = BaseModel
    pyd.Field = Field
    pyd.field_validator = field_validator
    pyd.model_validator = model_validator
    pyd.ValidationError = ValidationError
    sys.modules["pydantic"] = pyd

    # ---- 最小 fastapi 子集 ----
    fa = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code, detail=None):
            self.status_code = status_code; self.detail = detail

    class FastAPI:
        def __init__(self, *a, **k):
            self.routes = []

        def _add(self, method, path, fn):
            self.routes.append((method, path, fn))

        def get(self, path, *a, **k):
            return lambda fn: (self._add("GET", path, fn), fn)[1]

        def post(self, path, *a, **k):
            return lambda fn: (self._add("POST", path, fn), fn)[1]

        def put(self, path, *a, **k):
            return lambda fn: (self._add("PUT", path, fn), fn)[1]

        def delete(self, path, *a, **k):
            return lambda fn: (self._add("DELETE", path, fn), fn)[1]

        def on_event(self, *a, **k):
            return lambda fn: fn

        def mount(self, *a, **k):
            pass

    fa.FastAPI = FastAPI
    fa.HTTPException = HTTPException
    sys.modules["fastapi"] = fa

    responses_mod = types.ModuleType("fastapi.responses")
    class FileResponse:
        def __init__(self, path, *a, **k): self.path = path
    responses_mod.FileResponse = FileResponse
    sys.modules["fastapi.responses"] = responses_mod

    staticfiles_mod = types.ModuleType("fastapi.staticfiles")
    class StaticFiles:
        def __init__(self, *a, **k): pass
    staticfiles_mod.StaticFiles = StaticFiles
    sys.modules["fastapi.staticfiles"] = staticfiles_mod

    from app.main import app  # noqa: E402
    from app import database, engine  # noqa: E402
    database.init_db()

    import re as _re
    import inspect as _inspect

    def _convert(path, payload_model, raw):
        try:
            return payload_model(**raw), None
        except pyd.ValidationError as e:
            return None, (422, {"detail": e.errors_list})

    def _match_route(method, path):
        for m, pat, fn in app.routes:
            if m != method:
                continue
            if "{" not in pat:
                if pat == path:
                    return fn, {}
                continue
            rx = "^" + _re.sub(r"\{[^/}]+\}", r"(?P<__x>[^/]+)", pat) + "$"
            mm = _re.match(rx, path)
            if mm:
                names = _re.findall(r"\{([^/}]+)\}", pat)
                return fn, dict(zip(names, mm.groups()))
        return None

    async def _asgi_app(scope, receive, send):
        if scope["type"] != "http":
            return
        body = b""
        while True:
            m = await receive()
            body += m.get("body", b"")
            if not m.get("more_body"):
                break
        raw = json.loads(body.decode() or "null")
        matched = _match_route(scope["method"], scope["path"])
        status, out = 404, {"detail": "Not Found"}
        if matched:
            fn, path_params = matched
            kwargs = {}
            blocked = False
            fn_globals = getattr(fn, "__globals__", {})
            for name, ann in _inspect.get_annotations(fn, eval_str=False).items():
                if isinstance(ann, str):
                    try:
                        ann = eval(ann, fn_globals)
                    except Exception:
                        ann = None
                if isinstance(ann, type) and issubclass(ann, pyd.BaseModel):
                    model, err = _convert(scope["path"], ann, raw)
                    if err:
                        status, out = err
                        blocked = True
                        break
                    kwargs[name] = model          # 路由函数内部自行 .normalized()
                elif name in path_params:
                    kwargs[name] = int(path_params[name])
            if not blocked:
                try:
                    out = fn(**kwargs)
                    status = 200
                except fa.HTTPException as e:
                    status, out = e.status_code, {"detail": e.detail}
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type",
                                b"text/html; charset=utf-8" if _is_file_resp(out)
                                else b"application/json; charset=utf-8")]})
        if _is_file_resp(out):
            with open(out.path, "rb") as fh:
                payload = fh.read()
        else:
            payload = json.dumps(out, ensure_ascii=False, default=str).encode()
        await send({"type": "http.response.body", "body": payload})

    def _is_file_resp(x):
        return hasattr(x, "path") and x.__class__.__name__ == "FileResponse"

    class _StdlibResponse:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def json(self):
            return self._payload

    class _StdlibClient:
        """最小 ASGI 调用器：只支持本测试用到的 JSON GET/POST/PUT/DELETE。"""

        def _call_raw(self, method, path, body=None):
            raw = json.dumps(body).encode() if body is not None else b""
            scope = {
                "type": "http", "http_version": "1.1", "method": method,
                "scheme": "http", "path": path, "raw_path": path.encode(),
                "query_string": b"", "headers": [
                    (b"host", b"test"),
                    (b"content-type", b"application/json"),
                ],
                "client": ("test", 0), "server": ("test", 80),
            }
            chunks = [raw]
            messages = []

            async def receive():
                if chunks:
                    return {"type": "http.request", "body": chunks.pop(0)}
                return {"type": "http.disconnect"}

            async def send(m):
                messages.append(m)

            import asyncio
            asyncio.run(_asgi_app(scope, receive, send))
            status = next(m["status"] for m in messages if m["type"] == "http.response.start")
            data = b"".join(m.get("body", b"") for m in messages
                            if m["type"] == "http.response.body")
            return status, data

        def _call(self, method, path, body=None):
            status, data = self._call_raw(method, path, body)
            return _StdlibResponse(status, json.loads(data.decode() or "null"))

        def get_raw(self, path):
            return self._call_raw("GET", path)

        def get(self, path, **kw):
            resp = self._call("GET", path)
            if resp.status_code != 200 and resp.status_code in (404,):
                return resp
            return resp

        def post(self, path, json=None):
            return self._call("POST", path, json)

        def put(self, path, json=None):
            return self._call("PUT", path, json)

        def delete(self, path):
            return self._call("DELETE", path)

    client = _StdlibClient()
    BACKEND = "stdlib-asgi"

print("API test backend:", BACKEND)

DEMO_PARAMS = dict(
    name="12h 暴雨", capacity_m3=80000, initial_storage_m3=2000, dt_s=3600,
    q_safe=12, min_interval=2, initial_opening_index=1, initial_held=0,
    openings=[0, 3, 6, 9, 12, 16],
    inflows=[2, 5, 12, 20, 18, 12, 7, 4, 2, 1, 0, 0],
)

# 健康检查
assert client.get("/api/health").json() == {"ok": True}

# 页面
if BACKEND == "fastapi.testclient":
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
else:
    st_root, body_root = client.get_raw("/")
    assert st_root == 200 and "容量".encode() in body_root
    assert os.path.exists(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "static", "app.js"))

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
rp2 = client.post("/api/solve", json=dict(prec, openings=[0])).json()
assert not rp2["feasible"] and rp2["infeasible"]["code"] == "OVERFLOW_UNAVOIDABLE"
sid_p = client.post("/api/scenarios", json=prec).json()["id"]
run_p = client.post(f"/api/scenarios/{sid_p}/run").json()
assert run_p["feasible"] and run_p["summary"]["final_storage_m3"] == 99999.99994
hist_p = client.get(f"/api/runs/{run_p['run_id']}").json()
assert hist_p["summary"]["max_storage_m3"] <= hist_p["params"]["capacity_m3"]
client.delete(f"/api/scenarios/{sid_p}")

# 即席推演：规模超出资源保护上限 -> 可识别的规模拒绝
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
r_after = client.post("/api/solve", json=DEMO_PARAMS).json()
assert r_after["feasible"] and r_after["prune_stats"]["exact"] is True

# 参数校验：初始档位越界 -> 422
bad_params = dict(DEMO_PARAMS, initial_opening_index=99)
assert client.post("/api/solve", json=bad_params).status_code == 422
# 负容量 -> 422
assert client.post("/api/solve", json=dict(DEMO_PARAMS, capacity_m3=-1)).status_code == 422

# ---------- 容量反算 POST /api/size-capacity ----------

PLAN_PARAMS = {k: v for k, v in DEMO_PARAMS.items() if k != "capacity_m3"}

# (1) 预算 0、安全流量 12：可反算出内点最小库容，并带回见证与搜索区间
sp = dict(PLAN_PARAMS, q_safe=12, allowed_exceed_m3=0, capacity_upper_m3=80000)
sr = client.post("/api/size-capacity", json=sp).json()
assert sr["feasible"], sr
pl = sr["plan"]
assert pl["minimum_capacity_m3"] == 77600.0
assert pl["infeasible_below_m3"] == 77599.0
assert pl["feasible_at_or_above_m3"] == 77600.0
assert pl["quantum_m3"] == 1.0
assert pl["trials_count"] >= 2 and len(pl["trials"]) == pl["trials_count"]
# 见证序列：params.capacity_m3 即 C*；不溢流、不超预算、逐时段守恒
assert sr["params"]["capacity_m3"] == 77600.0
assert len(sr["steps"]) == 12
assert sr["summary"]["total_exceed_m3"] == 0.0
assert sr["summary"]["max_storage_m3"] <= 77600.0 + 1e-9
_v = sp["initial_storage_m3"]
for _s in sr["steps"]:
    assert abs(_s["storage_start_m3"] - _v) < 1e-6
    _v = _s["storage_start_m3"] + _s["inflow_m3"] - _s["release_m3"]
    assert abs(_s["storage_end_m3"] - _v) < 1e-6
    assert _s["storage_end_m3"] <= 77600.0 + 1e-9

# (2) 预算过紧 -> BUDGET_INFEASIBLE，带上界处见证
sb = client.post("/api/size-capacity",
                 json=dict(PLAN_PARAMS, q_safe=3, allowed_exceed_m3=1,
                           capacity_upper_m3=80000)).json()
assert not sb["feasible"] and sb["infeasible"]["code"] == "BUDGET_INFEASIBLE"
assert sb["witness"]["feasible"]
assert sb["witness"]["summary"]["total_exceed_m3"] > 1
assert sb["plan"]["minimum_capacity_m3"] is None
assert sb["plan"]["trials"][0]["verdict"] == "budget"

# (3) 上界物理无解 / 切换锁定 / 初始库容超上界
so = client.post("/api/size-capacity",
                 json=dict(PLAN_PARAMS, q_safe=12, min_interval=1, initial_held=1,
                           openings=[0, 3, 6, 9, 12], capacity_upper_m3=30000,
                           allowed_exceed_m3=0)).json()
assert not so["feasible"] and so["infeasible"]["code"] == "OVERFLOW_UNAVOIDABLE"
sl = client.post("/api/size-capacity",
                 json=dict(PLAN_PARAMS, q_safe=12, min_interval=3,
                           allowed_exceed_m3=0, capacity_upper_m3=80000)).json()
assert not sl["feasible"] and sl["infeasible"]["code"] == "LOCKED_INTERVAL"
# 初始库容超上界：接口层即 422（容量反算要求上界至少装得下初始库容）
sa_status = client.post("/api/size-capacity",
                        json=dict(PLAN_PARAMS, q_safe=12, initial_storage_m3=90000,
                                  allowed_exceed_m3=0, capacity_upper_m3=80000)).status_code
assert sa_status == 422

# (4) 分数量子：quantum 由输入精度决定（1e-6 m³）
sf = client.post("/api/size-capacity", json=dict(
    name="fq", initial_storage_m3=50.0, dt_s=5, q_safe=0.0, min_interval=1,
    initial_opening_index=1, initial_held=1,
    openings=[0, 0.000005], inflows=[0.000001] * 12,
    allowed_exceed_m3=0.000035, capacity_upper_m3=50.00005)).json()
assert sf["feasible"], sf
assert sf["plan"]["quantum_m3"] == 1e-6
assert sf["plan"]["minimum_capacity_m3"] == 50.000035
assert sf["plan"]["infeasible_below_m3"] == 50.000034

# (5) 任一试算触限 -> 整个请求 SEARCH_LIMIT，且未完成试算不进入区间
_calls = {"n": 0}
_orig = engine._search
def _flaky(*a, **k):
    _calls["n"] += 1
    if _calls["n"] == 3:
        raise engine.SearchLimitExceeded(12345, 4)
    return _orig(*a, **k)
engine._search = _flaky
try:
    sx = client.post("/api/size-capacity",
                     json=dict(PLAN_PARAMS, q_safe=10, allowed_exceed_m3=100000,
                               capacity_upper_m3=80000)).json()
finally:
    engine._search = _orig
assert not sx["feasible"] and sx["infeasible"]["code"] == "SEARCH_LIMIT"
assert "steps" not in sx and sx["plan"]["minimum_capacity_m3"] is None
assert len(sx["plan"]["trials"]) == 2          # 第 3 次未完成试算不入日志
assert sx["plan"]["feasible_at_or_above_m3"] == 40999.0
assert sx["plan"]["infeasible_below_m3"] == 1999.0

# (6) 入参校验：负预算 / 初始库容超上界 / 缺字段 -> 422
assert client.post("/api/size-capacity",
                   json=dict(sp, allowed_exceed_m3=-1)).status_code == 422
assert client.post("/api/size-capacity",
                   json=dict(sp, initial_storage_m3=90000)).status_code == 422
_missing = {k: v for k, v in sp.items() if k != "capacity_upper_m3"}
assert client.post("/api/size-capacity", json=_missing).status_code == 422

# 场景 CRUD + 持久化推演
sid = client.post("/api/scenarios", json=DEMO_PARAMS).json()["id"]
run = client.post(f"/api/scenarios/{sid}/run").json()
assert run["feasible"] and run["run_id"]
lst = client.get("/api/scenarios").json()
assert any(s["id"] == sid and s["last_feasible"] == 1 for s in lst)
runs = client.get(f"/api/scenarios/{sid}/runs").json()
assert len(runs) == 1
hist = client.get(f"/api/runs/{run['run_id']}").json()
assert hist["summary"]["total_exceed_m3"] == 0

client.put(f"/api/scenarios/{sid}", json=bad)
run2 = client.post(f"/api/scenarios/{sid}/run").json()
assert not run2["feasible"]
s = client.get(f"/api/scenarios/{sid}").json()
assert s["params"]["min_interval"] == 3
assert len(s["runs"]) == 2 and s["runs"][0]["feasible"] is False

assert client.get(f"/api/scenarios/{sid}").status_code == 200
assert client.get("/api/scenarios/99999").status_code == 404

client.delete(f"/api/scenarios/{sid}")
assert client.get(f"/api/runs/{run['run_id']}").status_code == 404

if os.path.exists(_db):
    os.remove(_db)
print("ALL API E2E TESTS PASSED")
