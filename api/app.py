"""API routes with injected services; importing this module makes no AWS calls."""

from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from time import perf_counter
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from api.auth import AuthError
from core.access import AccessDenied, resolve_owner
from core.csv_import import CsvError, parse_suite_csv
from core.gate import pass_rate
from core.jobqueue import JobQueue, expand_message
from core.metrics import emit
from core.models import COUNTERS, derive_status, new_run_id
from core.render import validate_template
from core.samples import PUBLIC_OWNER
from core.scorers import SCORER_VERSIONS
from core.settings import Settings
from core.store import IdempotencyConflict, Store

STATIC_DIR = Path(__file__).parent / "static"


class InferenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maxTokens: int = Field(default=512, ge=1, le=4096, strict=True)
    temperature: float = Field(default=0, ge=0, le=1)
    topP: float = Field(default=1, ge=0, le=1)
    stopSequences: list[str] = Field(default_factory=list, max_length=4)


class ConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "Untitled config"
    # What this version changes and why, for whoever reads the run later. It is stored
    # in the run's snapshot but never sent to the model, so the cache key ignores it.
    note: str = Field(default="", max_length=500)
    modelId: str
    system: str = ""
    template: str
    inferenceConfig: InferenceConfig = Field(default_factory=InferenceConfig)
    additionalModelRequestFields: dict | None = None


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configId: str
    configVersion: int = Field(gt=0)
    suiteId: str
    suiteVersion: int = Field(gt=0)
    mode: Literal["fresh", "replay", "auto"] = "replay"
    idempotencyKey: str = Field(min_length=1)


@dataclass(frozen=True)
class AppDeps:
    store: Store
    queue: JobQueue
    settings: Settings
    verify: Callable[[str], dict]
    clock: Callable[[], float]
    root_path: str = ""
    auth_mode: str = "cognito"


def create_app(deps: AppDeps) -> FastAPI:
    app = FastAPI(
        root_path=deps.root_path,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def current_owner(request: Request) -> str:
        unauthorized = HTTPException(
            status_code=401, detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise unauthorized
        try:
            claims = deps.verify(token.strip())
        except AuthError as exc:
            raise unauthorized from exc
        owner = claims.get("sub")
        if not isinstance(owner, str) or not owner:
            raise unauthorized
        return owner

    def checked_item(requester_sub, resource_id, read, *, write=False):
        # Every resource route uses this path: resolve ownership first, then read
        # only that owner's partition. Missing and inaccessible IDs both give 404.
        try:
            resource_owner = resolve_owner(requester_sub, resource_id, write=write)
        except AccessDenied as exc:
            raise HTTPException(status_code=404, detail="Not found") from exc
        item = read(resource_owner)
        if item is None:
            raise HTTPException(status_code=404, detail="Not found")
        return resource_owner, item

    def config_values(body: ConfigBody) -> dict:
        if body.modelId not in {model.id for model in deps.settings.allowed_models}:
            raise HTTPException(status_code=400, detail="Model is not allowed")
        try:
            validate_template(body.template)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return body.model_dump(exclude_none=True)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        response = await request_validation_exception_handler(request, exc)
        if any(tuple(error["loc"][:2]) == ("body", "inferenceConfig") for error in exc.errors()):
            response.status_code = 400
        return response

    @app.exception_handler(CsvError)
    async def csv_error(request: Request, exc: CsvError):
        return JSONResponse(status_code=400, content={"errors": exc.errors})

    # All later /api routes inherit this dependency. Handlers also use
    # Depends(current_owner) to obtain the same cached, verified sub.
    api = APIRouter(prefix="/api", dependencies=[Depends(current_owner)])

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/config.json")
    def config_json() -> dict[str, str]:
        return {
            "region": deps.settings.region,
            "clientId": deps.settings.client_id,
            "authMode": deps.auth_mode,
        }

    @api.get("/models")
    def models():
        return [asdict(model) for model in deps.settings.allowed_models]

    @api.post("/configs", status_code=201)
    def create_config(body: ConfigBody, owner: str = Depends(current_owner)):
        cfg = config_values(body)
        cfg_id = "cfg_" + uuid4().hex[:16]
        version = deps.store.put_config_version(owner, cfg_id, cfg)
        return {"configId": cfg_id, "version": version}

    @api.post("/configs/{config_id}/versions", status_code=201)
    def create_config_version(config_id: str, body: ConfigBody, owner: str = Depends(current_owner)):
        resource_owner, _ = checked_item(
            owner, config_id,
            lambda resolved: deps.store.get_config_version(resolved, config_id, 1),
            write=True,
        )
        version = deps.store.put_config_version(resource_owner, config_id, config_values(body))
        return {"configId": config_id, "version": version}

    @api.get("/configs")
    def configs(owner: str = Depends(current_owner)):
        return deps.store.list_configs(owner)

    @api.get("/configs/{config_id}/versions/{n}")
    def config_version(config_id: str, n: int, owner: str = Depends(current_owner)):
        _, cfg = checked_item(
            owner, config_id,
            lambda resolved: deps.store.get_config_version(resolved, config_id, n),
        )
        return cfg

    @api.post("/suites", status_code=201)
    def create_suite(
        csv_text: str = Body(media_type="text/csv"),
        name: str = "Imported suite",
        note: str = Query("", max_length=500),
        owner: str = Depends(current_owner),
    ):
        cases = parse_suite_csv(csv_text)
        suite_id = "s_" + uuid4().hex[:16]
        version = deps.store.put_suite_version(owner, suite_id, name, cases, note)
        return {"suiteId": suite_id, "version": version}

    @api.post("/suites/{suite_id}/versions", status_code=201)
    def create_suite_version(
        suite_id: str,
        csv_text: str = Body(media_type="text/csv"),
        name: str | None = None,
        note: str = Query("", max_length=500),
        owner: str = Depends(current_owner),
    ):
        resource_owner, first_version = checked_item(
            owner, suite_id,
            lambda resolved: deps.store.get_suite_version(resolved, suite_id, 1),
            write=True,
        )
        cases = parse_suite_csv(csv_text)
        version = deps.store.put_suite_version(
            resource_owner, suite_id, first_version["name"] if name is None else name, cases, note,
        )
        return {"suiteId": suite_id, "version": version}

    @api.get("/suites")
    def suites(owner: str = Depends(current_owner)):
        return deps.store.list_suites(owner) + deps.store.list_suites(PUBLIC_OWNER)

    @api.get("/suites/{suite_id}/versions/{n}/cases")
    def suite_cases(
        suite_id: str, n: int,
        cursor: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        owner: str = Depends(current_owner),
    ):
        resource_owner, _ = checked_item(
            owner, suite_id,
            lambda resolved: deps.store.get_suite_version(resolved, suite_id, n),
        )
        try:
            items, next_cursor = deps.store.query_cases(resource_owner, suite_id, n, cursor, limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid cursor") from exc
        # Store.query_cases already encodes its continuation key; don't encode twice.
        return {"items": items, "cursor": next_cursor}

    @api.post("/runs", status_code=202)
    def create_run(body: RunBody, owner: str = Depends(current_owner)):
        started = perf_counter()
        _, cfg = checked_item(
            owner, body.configId,
            lambda resolved: deps.store.get_config_version(resolved, body.configId, body.configVersion),
        )
        _, suite = checked_item(
            owner, body.suiteId,
            lambda resolved: deps.store.get_suite_version(resolved, body.suiteId, body.suiteVersion),
        )

        now = deps.clock()
        effective_mode = body.mode
        if effective_mode == "auto":
            pointer = deps.store.get_fresh_pointer(
                owner, body.configId, body.configVersion, body.suiteId, body.suiteVersion,
            )
            recent = pointer is not None and 0 <= now - pointer["created"] < deps.settings.cache_ttl_s
            effective_mode = "replay" if recent else "fresh"

        baseline_id = deps.store.get_baseline(owner, body.configId, body.suiteId, body.suiteVersion)
        baseline_rate = None
        if baseline_id is not None:
            baseline = deps.store.get_run(owner, baseline_id)
            baseline_rate = pass_rate(baseline["passed"], baseline["total"])

        # Hash the requested body, including its original mode. An auto-mode retry
        # may resolve differently after the first POST creates a fresh pointer.
        canonical = json.dumps(
            body.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        candidate = {
            "runId": new_run_id(owner, body.idempotencyKey),
            "requestHash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "cfgId": body.configId,
            "configVersion": body.configVersion,
            "config": cfg,
            "suiteId": body.suiteId,
            "suiteVersion": body.suiteVersion,
            "scorerVersions": SCORER_VERSIONS.copy(),
            "mode": body.mode,
            "effectiveMode": effective_mode,
            "created": now,
            "deadline": now + deps.settings.run_deadline_s,
            "total": suite["caseCount"],
            "expanded": 0,
            "expandCursor": None,
            "baselineRunId": baseline_id,
            "baselineRate": baseline_rate,
            **{counter: 0 for counter in COUNTERS},
        }
        try:
            run, created = deps.store.create_run(owner, candidate)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail="Idempotency key has a different request body") from exc

        # After a retry, use the stored snapshot, deadline, baseline and mode.
        if created or run["expanded"] == 0:
            try:
                # B5: the pointer write is inside the try too, so a store fault here also
                # gives the retryable 503. A retry rewrites the pointer (a plain put with the
                # stored values) in case the first attempt failed before writing it.
                if run["effectiveMode"] == "fresh":
                    deps.store.put_fresh_pointer(
                        owner, run["cfgId"], run["configVersion"], run["suiteId"],
                        run["suiteVersion"], run["runId"], run["created"],
                    )
                deps.queue.send(expand_message(owner, run["runId"]))
            except Exception:
                # Keep the run: retrying the same key can send its expand message.
                return JSONResponse(status_code=503, content={"runId": run["runId"], "retry": True})

        emit({"Service": "api"}, {"PostRunLatencyMs": ((perf_counter() - started) * 1000, "Milliseconds")})
        return {"runId": run["runId"]}

    def run_view(run: dict) -> dict:
        # Read-time fields only: neither status nor passRate is written back.
        return {key: value for key, value in run.items() if key not in {"requestHash", "expandCursor"}} | {
            "status": derive_status(run, deps.clock()),
            "passRate": pass_rate(run["passed"], run["total"]),
        }

    @api.get("/runs")
    def runs(owner: str = Depends(current_owner)):
        return [run_view(run) for run in deps.store.list_runs(owner)]

    @api.get("/runs/{run_id}")
    def run_details(run_id: str, owner: str = Depends(current_owner)):
        _, run = checked_item(
            owner, run_id, lambda resolved: deps.store.get_run(resolved, run_id),
        )
        return run_view(run)

    @api.get("/runs/{run_id}/results")
    def run_results(
        run_id: str,
        cursor: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        owner: str = Depends(current_owner),
    ):
        resource_owner, _ = checked_item(
            owner, run_id, lambda resolved: deps.store.get_run(resolved, run_id),
        )
        try:
            items, next_cursor = deps.store.query_results(resource_owner, run_id, cursor, limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid cursor") from exc
        return {"items": items, "cursor": next_cursor}

    @api.put("/runs/{run_id}/baseline")
    def mark_baseline(run_id: str, owner: str = Depends(current_owner)):
        resource_owner, run = checked_item(
            owner, run_id, lambda resolved: deps.store.get_run(resolved, run_id), write=True,
        )
        if (
            derive_status(run, deps.clock()) != "COMPLETE"
            or run["effectiveMode"] != "fresh"
            or run["errored"] != 0
        ):
            raise HTTPException(status_code=409, detail="Baseline requires a complete fresh run with no errors")
        deps.store.set_baseline(resource_owner, run)
        return {"runId": run["runId"]}

    @api.get("/baselines/{config_id}/{suite_id}/{version}")
    def baseline(config_id: str, suite_id: str, version: int, owner: str = Depends(current_owner)):
        checked_item(
            owner, config_id,
            lambda resolved: deps.store.get_config_version(resolved, config_id, 1),
        )
        checked_item(
            owner, suite_id,
            lambda resolved: deps.store.get_suite_version(resolved, suite_id, version),
        )
        return {"runId": deps.store.get_baseline(owner, config_id, suite_id, version)}

    app.include_router(api)
    # Last, so /health, /config.json and /api/* win over the catch-all static mount.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
