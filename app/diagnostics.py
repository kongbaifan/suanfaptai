"""服务自诊断

只读汇总运行环境、发布资源完整性、数据规模与实时仿真状态。
所有字段由后端当场探测，不依赖任何外部进程或日志文件。
"""

from __future__ import annotations

import platform
import sys
import time
from typing import Literal

from pydantic import BaseModel

from app import realtime
from app.data.catalog_quality import build_catalog_quality_report
from app.data.equipment import load_equipment_catalog
from app.data.project_store import PROJECT_DIR
from app.equipment_knowledge import load_equipment_knowledge
from app.release import (
    APP_VERSION,
    PRODUCT_NAME,
    PROJECT_SCHEMA_VERSION,
    run_diagnostics,
)

_STARTED_AT = time.time()


class DiagnosticCheckView(BaseModel):
    """单项检查结果。`required=False` 的项只作参考，不影响总体状态。"""

    name: str
    ok: bool
    required: bool
    detail: str


class ServiceInfo(BaseModel):
    product: str
    app_version: str
    project_schema_version: str
    python_version: str
    platform: str
    frozen: bool
    uptime_seconds: float


class DataSummary(BaseModel):
    """数据规模。数值为当场读取，异常时为 -1 而不是抛错。"""

    catalog_items: int
    catalog_types: int
    catalog_issues: int
    knowledge_entries: int
    stored_projects: int


class RealtimeProjectStat(BaseModel):
    project_id: str
    tick: int
    running: bool
    subscribers: int
    updated_at: str | None = None


class RealtimeSummary(BaseModel):
    active_sessions: int
    running_simulations: int
    websocket_subscribers: int
    projects: list[RealtimeProjectStat]


class DiagnosticsResponse(BaseModel):
    status: Literal["ok", "error"]
    service: ServiceInfo
    checks: list[DiagnosticCheckView]
    data: DataSummary
    realtime: RealtimeSummary


def read_diagnostics() -> DiagnosticsResponse:
    report = run_diagnostics()
    return DiagnosticsResponse(
        status="ok" if report.ok else "error",
        service=ServiceInfo(
            product=PRODUCT_NAME,
            app_version=APP_VERSION,
            project_schema_version=PROJECT_SCHEMA_VERSION,
            python_version=platform.python_version(),
            platform=f"{platform.system()} {platform.release()}",
            frozen=bool(getattr(sys, "frozen", False)),
            uptime_seconds=round(time.time() - _STARTED_AT, 3),
        ),
        checks=[
            DiagnosticCheckView(
                name=check.name,
                ok=check.ok,
                required=check.required,
                detail=check.detail,
            )
            for check in report.checks
        ],
        data=_data_summary(),
        realtime=RealtimeSummary.model_validate(realtime.diagnostics_snapshot()),
    )


def _data_summary() -> DataSummary:
    return DataSummary(
        catalog_items=_safe_count(lambda: len(load_equipment_catalog())),
        catalog_types=_safe_count(
            lambda: len({str(item.get("type", "")) for item in load_equipment_catalog()})
        ),
        catalog_issues=_safe_count(
            lambda: len(build_catalog_quality_report().get("issues", []))
        ),
        knowledge_entries=_safe_count(lambda: len(load_equipment_knowledge())),
        stored_projects=_safe_count(
            lambda: len(list(PROJECT_DIR.glob("*.json"))) if PROJECT_DIR.is_dir() else 0
        ),
    )


def _safe_count(read) -> int:
    """诊断页不能因为某一项数据损坏就整体 500，读不到的项返回 -1。"""
    try:
        return int(read())
    except (OSError, ValueError, TypeError, KeyError):
        return -1
