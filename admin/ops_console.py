from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from . import app as backend

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_LOG_DIR = PROJECT_ROOT / "logs" / "profiles"
ALL_PROFILES_SCRIPT = PROJECT_ROOT / "scripts" / "all-profiles.sh"

_REPLACED_ROUTES = {
    ("/api/run/apply-vacancies", "POST"),
    ("/api/run/apply-vacancies-full", "POST"),
    ("/api/run/reply-employers", "POST"),
    ("/api/run/update-resumes", "POST"),
}

_LOG_SOURCE_LABELS = {
    "cli": "CLI / библиотека",
    "apply": "Автоотклики",
    "reply": "Автоответы",
    "daily": "Полный проход",
    "update": "Обновление резюме",
    "boost": "Поднятие резюме",
    "refresh": "Обновление токена",
    "cron": "Cron scheduler",
    "ops-daily": "Ops daily report",
}


class CanonicalApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str = "default"
    dry_run: bool = True
    confirm_live: bool = False
    search: str = Field("", max_length=500)
    resume_id: str = Field("", max_length=256)
    max_responses: int = Field(100, ge=1, le=1_000)
    per_page: int = Field(50, ge=1, le=100)
    total_pages: int = Field(20, ge=1, le=100)
    response_delay: str = Field("1-3", max_length=32)
    timeout: int = Field(3_600, ge=30, le=14_400)


class CanonicalReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str = "default"
    dry_run: bool = True
    confirm_live: bool = False
    max_chats: int = Field(100, ge=1, le=1_000)


class CanonicalDailyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str = "default"
    dry_run: bool = True
    confirm_live: bool = False
    search: str = Field("", max_length=500)
    max_responses: int = Field(100, ge=1, le=1_000)
    per_page: int = Field(50, ge=1, le=100)
    total_pages: int = Field(20, ge=1, le=100)
    max_chats: int = Field(100, ge=1, le=1_000)


class CanonicalUtilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str = "default"
    confirm_live: bool = False


def _route_is_replaced(route: object) -> bool:
    path = getattr(route, "path", None)
    methods = getattr(route, "methods", None) or set()
    return any(path == wanted_path and method in methods for wanted_path, method in _REPLACED_ROUTES)


def _profile_log_path(profile: str, source: str) -> Path:
    profile = backend._validate_profile_name(profile)
    if source == "cli":
        return backend._log_path(profile)
    if source in {"apply", "reply", "daily", "update", "boost", "refresh"}:
        return PROFILE_LOG_DIR / f"{profile}-{source}.log"
    if source == "cron":
        return PROJECT_ROOT / "logs" / "cron.log"
    if source == "ops-daily":
        return PROJECT_ROOT / "logs" / "ops-daily.log"
    raise HTTPException(400, f"Unknown log source: {source}")


def _file_updated_at(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _tail_text(path: Path, *, lines: int, start_offset: int | None = None) -> tuple[str, int]:
    if not path.exists():
        return "", 0

    size = path.stat().st_size
    offset = start_offset or 0
    if offset < 0 or offset > size:
        offset = 0

    # A single admin response should never read an unbounded historical log.
    max_bytes = 2 * 1024 * 1024
    read_from = max(offset, size - max_bytes)
    with open(path, "rb") as stream:
        stream.seek(read_from)
        raw = stream.read(max_bytes)
    text = raw.decode("utf-8", errors="replace")
    selected = text.splitlines(keepends=True)[-lines:]
    return "".join(selected), size


def _history_path(profile: str) -> Path:
    return backend._operation_history_path(profile)


def _persist_history(record: dict[str, Any]) -> None:
    keys = (
        "op_id",
        "operation",
        "profile",
        "dry_run",
        "mode",
        "started_at",
        "finished_at",
        "returncode",
        "cancelled",
        "error",
        "command_preview",
        "log_source",
        "log_offset",
        "log_end_offset",
    )
    payload = {key: record[key] for key in keys if key in record}
    path = _history_path(record["profile"])
    if not path.parent.exists():
        return
    try:
        with backend.operation_history_lock, open(path, "a", encoding="utf-8") as history:
            history.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _find_history_record(profile: str, op_id: str) -> dict[str, Any] | None:
    path = _history_path(profile)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as history:
            for raw in reversed(history.readlines()):
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if item.get("profile") == profile and item.get("op_id") == op_id:
                    return item
    except OSError:
        return None
    return None


def _finalize_wrapper_operation(op_id: str, outcome: dict[str, Any]) -> None:
    history_record: dict[str, Any] | None = None
    with backend.operations_lock:
        record = backend.running_operations.get(op_id)
        if not record or record.get("_finalized"):
            return
        log_path = _profile_log_path(record["profile"], record["log_source"])
        record.update(
            {
                "completed": True,
                "running": False,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "process": None,
                "log_end_offset": log_path.stat().st_size if log_path.exists() else 0,
            }
        )
        record.update(outcome)
        record["_finalized"] = True
        backend.active_operations.pop((record["profile"], record["operation"]), None)
        history_record = dict(record)
    if history_record:
        _persist_history(history_record)


def _start_wrapper_operation(
    *,
    profile: str,
    operation: str,
    wrapper_command: str,
    args: list[str],
    dry_run: bool,
    confirm_live: bool,
) -> dict[str, Any]:
    profile = backend._validate_profile_name(profile)
    if not dry_run and operation in {"apply", "reply", "daily", "update", "boost"} and not confirm_live:
        raise HTTPException(409, "Live operation requires confirm_live=true.")
    if not ALL_PROFILES_SCRIPT.exists():
        raise HTTPException(500, "scripts/all-profiles.sh is missing")

    log_path = _profile_log_path(profile, wrapper_command)
    log_offset = log_path.stat().st_size if log_path.exists() else 0
    op_id = str(uuid4())[:8]
    command = ["bash", str(ALL_PROFILES_SCRIPT), wrapper_command, *args]
    command_preview = shlex.join(["scripts/all-profiles.sh", wrapper_command, *args])

    with backend.operations_lock:
        conflicting = next(
            (
                item
                for item in backend.running_operations.values()
                if item.get("profile") == profile and item.get("running")
            ),
            None,
        )
        if conflicting:
            raise HTTPException(
                409,
                f"Profile {profile} already has a running operation "
                f"{conflicting.get('operation')} ({conflicting.get('op_id')}).",
            )
        backend.active_operations[(profile, operation)] = op_id
        backend.running_operations[op_id] = {
            "op_id": op_id,
            "operation": operation,
            "profile": profile,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "running": True,
            "completed": False,
            "process": None,
            "dry_run": dry_run,
            "mode": "dry-run" if dry_run else ("utility" if operation == "refresh" else "live"),
            "command_preview": command_preview,
            "log_source": wrapper_command,
            "log_offset": log_offset,
        }

    def execute() -> None:
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            env["CONFIG_DIR"] = str(backend._config_root())
            # Force exactly one account while still using the same all-profiles
            # wrapper, file lock and persistent log format as cron.
            env["HH_ONLY_PROFILE"] = profile
            env["HH_FAIL_ON_LOCKED_PROFILE"] = "1"

            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(PROJECT_ROOT),
                env=env,
                start_new_session=True,
            )
            with backend.operations_lock:
                record = backend.running_operations.get(op_id)
                cancellation_requested = not record or record.get("cancel_requested")
                if record:
                    record["process"] = process
            if cancellation_requested:
                backend._terminate_operation_process(process)

            stdout, stderr = process.communicate()
            returncode = process.returncode if process.returncode is not None else 1
            _finalize_wrapper_operation(
                op_id,
                {
                    "returncode": int(returncode),
                    "cancelled": bool(cancellation_requested),
                    "stdout": stdout[-backend.constants.ADMIN_LOG_OUTPUT_LIMIT :] if stdout else "",
                    "stderr": stderr[-backend.constants.ADMIN_LOG_ERROR_LIMIT :] if stderr else "",
                },
            )
        except Exception as ex:  # pragma: no cover - defensive runtime boundary
            _finalize_wrapper_operation(
                op_id,
                {
                    "returncode": 1,
                    "error": str(ex),
                    "stdout": "",
                    "stderr": str(ex),
                },
            )

    threading.Thread(target=execute, daemon=True).start()
    return {
        "op_id": op_id,
        "profile": profile,
        "operation": operation,
        "dry_run": dry_run,
        "mode": "dry-run" if dry_run else ("utility" if operation == "refresh" else "live"),
        "command_preview": command_preview,
        "log_source": wrapper_command,
    }


def _mode_args(dry_run: bool, confirm_live: bool) -> list[str]:
    if dry_run:
        return ["--dry-run"]
    if not confirm_live:
        raise HTTPException(409, "Live operation requires confirm_live=true.")
    return ["--live"]


def _apply_args(body: CanonicalApplyRequest) -> list[str]:
    args = _mode_args(body.dry_run, body.confirm_live)
    if body.search:
        args += ["--search", body.search]
    if body.resume_id:
        if body.resume_id.startswith("-"):
            raise HTTPException(422, "Invalid resume id")
        args += ["--resume-id", body.resume_id]
    args += [
        "--limit",
        str(body.max_responses),
        "--per-page",
        str(body.per_page),
        "--pages",
        str(body.total_pages),
        "--response-delay",
        body.response_delay,
        "--timeout",
        str(body.timeout),
    ]
    return args


def _daily_args(body: CanonicalDailyRequest) -> list[str]:
    args = _mode_args(body.dry_run, body.confirm_live)
    if body.search:
        args += ["--search", body.search]
    args += [
        "--limit",
        str(body.max_responses),
        "--per-page",
        str(body.per_page),
        "--pages",
        str(body.total_pages),
        "--chats",
        str(body.max_chats),
    ]
    return args


def _config_summary(profile: str) -> dict[str, Any]:
    profile = backend._validate_profile_name(profile)
    config_path = backend._config_path(profile)
    config: dict[str, Any] = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            config = {}
    fallback = config.get("cover_letter_fallback") or {}
    ai = config.get("openai_cover_letter") or {}
    return {
        "cover_letter_fallback": {
            "enabled": bool(fallback.get("enabled", True)),
            "configured": bool(str(fallback.get("message") or "").strip()),
        },
        "cover_letter_ai": {
            "configured": bool(ai.get("api_key")),
            "model": ai.get("model"),
        },
    }


def _runtime_defaults(profile: str) -> dict[str, Any]:
    hard_filter = Path(os.getenv("APPLY_HARD_FILTER_FILE", str(PROJECT_ROOT / "rules" / "apply-hard-filter.regex")))
    prompt = Path(os.getenv("SYSTEM_PROMPT", str(PROJECT_ROOT / "prompts" / "cover_letter_frontend.txt")))
    return {
        "search": os.getenv("SEARCH_QUERY", "Frontend разработчик"),
        "max_responses": int(os.getenv("APPLY_LIMIT", "100")),
        "per_page": int(os.getenv("APPLY_PER_PAGE", "50")),
        "total_pages": int(os.getenv("APPLY_PAGES", "20")),
        "timeout": int(os.getenv("APPLY_RUN_TIMEOUT", "3600")),
        "response_delay": os.getenv("APPLY_RESPONSE_DELAY", "1-3"),
        "max_chats": int(os.getenv("REPLY_CHATS", "100")),
        "hard_filter": str(hard_filter.relative_to(PROJECT_ROOT)) if hard_filter.is_relative_to(PROJECT_ROOT) else str(hard_filter),
        "cover_prompt": str(prompt.relative_to(PROJECT_ROOT)) if prompt.is_relative_to(PROJECT_ROOT) else str(prompt),
        **_config_summary(profile),
    }


def _live_hh_overview(profile: str) -> dict[str, Any]:
    checked_at = datetime.now(timezone.utc).isoformat()
    calls = {
        "me": ("/me", None),
        "negotiations": ("/negotiations", {"status": "active", "page": 0, "per_page": 1}),
        "resumes": ("/resumes/mine", None),
    }
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        future_map = {
            pool.submit(backend._hh_get, profile, path, params): name
            for name, (path, params) in calls.items()
        }
        for future in as_completed(future_map):
            name = future_map[future]
            try:
                results[name] = future.result()
            except Exception as ex:  # HTTPException included; expose no secrets
                detail = getattr(ex, "detail", str(ex))
                errors[name] = str(detail)[:300]

    negotiations = results.get("negotiations") or {}
    resumes = results.get("resumes") or {}
    me = results.get("me") or {}
    return {
        "reachable": "me" in results,
        "checked_at": checked_at,
        "active_negotiations": negotiations.get("found", len(negotiations.get("items", []))),
        "resumes": len(resumes.get("items", [])),
        "identity": " ".join(filter(None, [me.get("first_name"), me.get("last_name")])).strip() or me.get("email"),
        "errors": errors,
    }


def _recent_history(profile: str, limit: int = 8) -> list[dict[str, Any]]:
    path = _history_path(profile)
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as history:
            for raw in history:
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if item.get("profile") == profile:
                    items.append(item)
    except OSError:
        return []
    items.sort(key=lambda item: item.get("started_at") or "", reverse=True)
    return items[:limit]


def install_ops_routes(target: FastAPI) -> FastAPI:
    """Install curated production-parity controls; never expose a generic shell."""
    target.router.routes[:] = [route for route in target.router.routes if not _route_is_replaced(route)]

    @target.get("/api/ops/defaults")
    def ops_defaults(profile: str = Query("default")):
        return _runtime_defaults(backend._validate_profile_name(profile))

    @target.get("/api/ops/overview")
    def ops_overview(profile: str = Query("default")):
        profile = backend._validate_profile_name(profile)
        snapshot = backend._snapshot_updated_at(profile)
        return {
            "profile": profile,
            "local_snapshot_at": snapshot,
            "token": backend._get_token_info(profile),
            "live_hh": _live_hh_overview(profile),
            "recent_operations": _recent_history(profile, limit=8),
            "defaults": _runtime_defaults(profile),
        }

    @target.get("/api/ops/logs")
    def ops_logs(
        profile: str = Query("default"),
        source: str = Query("apply"),
        lines: int = Query(300, ge=1, le=2_000),
    ):
        profile = backend._validate_profile_name(profile)
        path = _profile_log_path(profile, source)
        text, size = _tail_text(path, lines=lines)
        return {
            "profile": profile,
            "source": source,
            "label": _LOG_SOURCE_LABELS[source],
            "exists": path.exists(),
            "updated_at": _file_updated_at(path),
            "size": size,
            "lines": text.splitlines(keepends=True),
        }

    @target.get("/api/ops/log-sources")
    def ops_log_sources(profile: str = Query("default")):
        profile = backend._validate_profile_name(profile)
        return {
            "sources": [
                {
                    "id": source,
                    "label": label,
                    "exists": _profile_log_path(profile, source).exists(),
                    "updated_at": _file_updated_at(_profile_log_path(profile, source)),
                }
                for source, label in _LOG_SOURCE_LABELS.items()
            ]
        }

    @target.get("/api/ops/operations")
    def ops_operations(profile: str = Query("default")):
        profile = backend._validate_profile_name(profile)
        with backend.operations_lock:
            records = [
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"process", "stdout", "stderr"} and not key.startswith("_")
                }
                for item in backend.running_operations.values()
                if item.get("profile") == profile
            ]
        records.sort(key=lambda item: item.get("started_at") or "", reverse=True)
        return {"profile": profile, "operations": records}

    @target.get("/api/ops/status/{op_id}")
    def ops_status(
        op_id: str,
        profile: str = Query("default"),
        lines: int = Query(400, ge=1, le=2_000),
    ):
        profile = backend._validate_profile_name(profile)
        with backend.operations_lock:
            current = backend.running_operations.get(op_id)
            record = dict(current) if current and current.get("profile") == profile else None
        if record is None:
            record = _find_history_record(profile, op_id)
        if record is None:
            raise HTTPException(404, f"Operation {op_id} was not found for this profile")

        source = record.get("log_source")
        log_text = ""
        log_updated_at = None
        if source in _LOG_SOURCE_LABELS:
            path = _profile_log_path(profile, source)
            log_text, _ = _tail_text(path, lines=lines, start_offset=int(record.get("log_offset") or 0))
            log_updated_at = _file_updated_at(path)

        return {
            "op_id": op_id,
            "profile": profile,
            "operation": record.get("operation"),
            "mode": record.get("mode"),
            "dry_run": record.get("dry_run", False),
            "running": record.get("running", False),
            "returncode": record.get("returncode"),
            "cancelled": record.get("cancelled", False),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "command_preview": record.get("command_preview"),
            "log_source": source,
            "log_updated_at": log_updated_at,
            "log_text": log_text,
            "stdout": record.get("stdout", ""),
            "stderr": record.get("stderr", ""),
            "error": record.get("error"),
        }

    @target.post("/api/ops/cancel/{op_id}")
    def ops_cancel(op_id: str, profile: str = Query("default")):
        profile = backend._validate_profile_name(profile)
        with backend.operations_lock:
            record = backend.running_operations.get(op_id)
            if not record or record.get("profile") != profile or not record.get("running"):
                raise HTTPException(404, "Running operation was not found")
            record["cancel_requested"] = True
            process = record.get("process")
        if process is not None:
            backend._terminate_operation_process(process)
            _finalize_wrapper_operation(
                op_id,
                {
                    "cancelled": True,
                    "returncode": process.returncode if process.returncode is not None else -signal.SIGTERM,
                    "stderr": "Cancelled by admin user",
                },
            )
        return {"ok": True, "op_id": op_id}

    @target.post("/api/ops/run/apply")
    def ops_run_apply(body: CanonicalApplyRequest):
        return _start_wrapper_operation(
            profile=body.profile,
            operation="apply",
            wrapper_command="apply",
            args=_apply_args(body),
            dry_run=body.dry_run,
            confirm_live=body.confirm_live,
        )

    @target.post("/api/ops/run/reply")
    def ops_run_reply(body: CanonicalReplyRequest):
        args = _mode_args(body.dry_run, body.confirm_live) + ["--chats", str(body.max_chats)]
        return _start_wrapper_operation(
            profile=body.profile,
            operation="reply",
            wrapper_command="reply",
            args=args,
            dry_run=body.dry_run,
            confirm_live=body.confirm_live,
        )

    @target.post("/api/ops/run/daily")
    def ops_run_daily(body: CanonicalDailyRequest):
        return _start_wrapper_operation(
            profile=body.profile,
            operation="daily",
            wrapper_command="daily",
            args=_daily_args(body),
            dry_run=body.dry_run,
            confirm_live=body.confirm_live,
        )

    @target.post("/api/ops/run/update")
    def ops_run_update(body: CanonicalUtilityRequest):
        if not body.confirm_live:
            raise HTTPException(409, "Updating resumes requires confirm_live=true.")
        return _start_wrapper_operation(
            profile=body.profile,
            operation="update",
            wrapper_command="update",
            args=["--live"],
            dry_run=False,
            confirm_live=True,
        )

    @target.post("/api/ops/run/boost")
    def ops_run_boost(body: CanonicalUtilityRequest):
        if not body.confirm_live:
            raise HTTPException(409, "Boosting resumes requires confirm_live=true.")
        return _start_wrapper_operation(
            profile=body.profile,
            operation="boost",
            wrapper_command="boost",
            args=["--live"],
            dry_run=False,
            confirm_live=True,
        )

    @target.post("/api/ops/run/refresh")
    def ops_run_refresh(body: CanonicalUtilityRequest):
        return _start_wrapper_operation(
            profile=body.profile,
            operation="refresh",
            wrapper_command="refresh",
            args=[],
            dry_run=False,
            confirm_live=True,
        )

    # Backward-compatible UI endpoints now delegate to the canonical wrappers.
    @target.post("/api/run/apply-vacancies")
    def legacy_run_apply(body: backend.RunRequest):
        defaults = _runtime_defaults(body.profile)
        request = CanonicalApplyRequest(
            profile=body.profile,
            dry_run=body.dry_run,
            confirm_live=body.confirm_live,
            max_responses=defaults["max_responses"],
            per_page=defaults["per_page"],
            total_pages=defaults["total_pages"],
            response_delay=body.response_delay,
            timeout=defaults["timeout"],
        )
        return ops_run_apply(request)

    @target.post("/api/run/reply-employers")
    def legacy_run_reply(body: backend.ReplyEmployersRequest):
        if not body.use_ai or body.only_invitations or body.reply_message or body.system_prompt or body.message_prompt:
            raise HTTPException(
                422,
                "Legacy reply options are not part of the production reply.sh path. "
                "Use /api/ops/run/reply.",
            )
        return ops_run_reply(
            CanonicalReplyRequest(
                profile=body.profile,
                dry_run=body.dry_run,
                confirm_live=body.confirm_live,
                max_chats=min(1_000, max(1, body.max_pages * 10)),
            )
        )

    @target.post("/api/run/update-resumes")
    def legacy_run_update(body: backend.RunRequest):
        return ops_run_update(
            CanonicalUtilityRequest(profile=body.profile, confirm_live=body.confirm_live)
        )

    @target.post("/api/run/apply-vacancies-full")
    def legacy_full_apply_disabled():
        raise HTTPException(
            410,
            "The legacy advanced apply endpoint bypassed the production safety wrapper and is disabled. "
            "Use /api/ops/run/apply.",
        )

    return target
