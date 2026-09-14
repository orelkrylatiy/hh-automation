from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import admin.ops_console as ops_console


def _client() -> TestClient:
    """Test operational routes without mutating the legacy backend app."""
    return TestClient(ops_console.install_ops_routes(FastAPI()))


def test_apply_args_match_production_wrapper_contract():
    body = ops_console.CanonicalApplyRequest(
        profile="default",
        dry_run=True,
        search="React TypeScript",
        resume_id="resume-123",
        max_responses=17,
        per_page=42,
        total_pages=3,
        response_delay="10-20",
        timeout=777,
    )

    args = ops_console._apply_args(body)

    assert args == [
        "--dry-run",
        "--search",
        "React TypeScript",
        "--resume-id",
        "resume-123",
        "--limit",
        "17",
        "--per-page",
        "42",
        "--pages",
        "3",
        "--response-delay",
        "10-20",
        "--timeout",
        "777",
    ]


def test_live_apply_requires_explicit_confirmation():
    body = ops_console.CanonicalApplyRequest(
        profile="default",
        dry_run=False,
        confirm_live=False,
    )

    with pytest.raises(Exception) as error:
        ops_console._apply_args(body)

    assert getattr(error.value, "status_code", None) == 409


def test_log_sources_are_allowlisted(tmp_path, monkeypatch):
    monkeypatch.setattr(ops_console, "PROFILE_LOG_DIR", tmp_path / "profiles")
    monkeypatch.setattr(ops_console, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        ops_console.backend,
        "_log_path",
        lambda profile: tmp_path / profile / "log.txt",
    )

    assert (
        ops_console._profile_log_path("default", "apply")
        == tmp_path / "profiles" / "default-apply.log"
    )
    assert ops_console._profile_log_path("default", "cron") == tmp_path / "logs" / "cron.log"

    with pytest.raises(Exception) as error:
        ops_console._profile_log_path("default", "../../etc/passwd")
    assert getattr(error.value, "status_code", None) == 400


def test_admin_apply_endpoint_delegates_to_all_profiles_wrapper(monkeypatch):
    captured = {}

    def fake_start(**kwargs):
        captured.update(kwargs)
        return {"op_id": "test-op", **kwargs}

    monkeypatch.setattr(ops_console, "_start_wrapper_operation", fake_start)
    client = _client()

    response = client.post(
        "/api/ops/run/apply",
        json={
            "profile": "default",
            "dry_run": True,
            "search": "Frontend developer",
            "max_responses": 7,
            "per_page": 25,
            "total_pages": 2,
            "response_delay": "2-4",
            "timeout": 600,
        },
    )

    assert response.status_code == 200
    assert captured["operation"] == "apply"
    assert captured["wrapper_command"] == "apply"
    assert captured["dry_run"] is True
    assert captured["args"][0] == "--dry-run"
    assert "--search" in captured["args"]
    assert "--response-delay" in captured["args"]


def test_admin_reply_endpoint_uses_production_reply_wrapper(monkeypatch):
    captured = {}

    def fake_start(**kwargs):
        captured.update(kwargs)
        return {"op_id": "reply-op", **kwargs}

    monkeypatch.setattr(ops_console, "_start_wrapper_operation", fake_start)
    client = _client()

    response = client.post(
        "/api/ops/run/reply",
        json={"profile": "default", "dry_run": True, "max_chats": 23},
    )

    assert response.status_code == 200
    assert captured["wrapper_command"] == "reply"
    assert captured["args"] == ["--dry-run", "--chats", "23"]


def test_legacy_full_apply_endpoint_is_disabled():
    client = _client()

    response = client.post("/api/run/apply-vacancies-full", json={})

    assert response.status_code == 410
    assert "production safety wrapper" in response.json()["detail"]


def test_runtime_scripts_expose_admin_parity_controls():
    root = Path(__file__).resolve().parents[1]
    apply_script = (root / "scripts" / "apply.sh").read_text(encoding="utf-8")
    all_profiles = (root / "scripts" / "all-profiles.sh").read_text(encoding="utf-8")

    assert "apply-safe" in apply_script
    assert "--response-delay" in apply_script
    assert "--resume-id" in apply_script
    assert "HH_ONLY_PROFILE" in all_profiles
    assert "HH_FAIL_ON_LOCKED_PROFILE" in all_profiles
    assert "HH_RUN_START" in all_profiles
    assert "HH_RUN_END" in all_profiles
