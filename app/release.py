"""Release metadata, resource discovery, and startup diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

APP_VERSION = "0.1.0"
PROJECT_SCHEMA_VERSION = "1.0"
PRODUCT_NAME = "HVACAlgorithmService"


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    name: str
    ok: bool
    required: bool
    detail: str


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    status: str
    app_version: str
    project_schema_version: str
    frozen: bool
    data_dir: str
    checks: tuple[DiagnosticCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks if check.required)

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["ok"] = self.ok
        return result


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("PROGRAMDATA")
    if base:
        return Path(base) / PRODUCT_NAME / "data"
    return Path.cwd() / "data"


def default_log_dir() -> Path:
    data_dir = default_data_dir()
    return data_dir.parent / "logs"


def resource_path(relative: str | Path) -> Path:
    """Resolve a bundled path while retaining source-tree compatibility."""

    relative_path = Path(relative)
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root is not None:
        return Path(frozen_root) / relative_path

    project_root = Path(__file__).resolve().parents[1]
    local = project_root / relative_path
    if local.exists():
        return local
    return project_root.parent / relative_path


def load_release_manifest() -> dict[str, Any]:
    path = resource_path("app/release_manifest.json")
    with path.open("r", encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        # 必须保持 ValueError：`run_diagnostics` 捕获
        # `(OSError, ValueError, json.JSONDecodeError)` 并把异常降级成一条
        # DiagnosticCheck。换成 TypeError 会越过该捕获，让诊断接口直接崩溃。
        raise ValueError("release manifest must contain a JSON object")  # noqa: TRY004
    return value


def version_info() -> dict[str, object]:
    manifest = load_release_manifest()
    return {
        "product": PRODUCT_NAME,
        "app_version": APP_VERSION,
        "project_schema_version": PROJECT_SCHEMA_VERSION,
        "build_mode": manifest.get("build_mode"),
    }


def run_diagnostics(data_dir: Path | None = None) -> DiagnosticReport:
    target_data_dir = (data_dir or default_data_dir()).resolve()
    checks: list[DiagnosticCheck] = []

    manifest: dict[str, Any] = {}
    try:
        manifest = load_release_manifest()
        versions_match = (
            manifest.get("app_version") == APP_VERSION
            and manifest.get("project_schema_version") == PROJECT_SCHEMA_VERSION
        )
        checks.append(
            DiagnosticCheck(
                "release_manifest",
                versions_match,
                True,
                "version fields match runtime constants"
                if versions_match
                else "manifest and runtime version fields differ",
            )
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        checks.append(
            DiagnosticCheck(
                "release_manifest", False, True, f"{type(error).__name__}: {error}"
            )
        )

    resources = manifest.get("resources", [])
    if not isinstance(resources, list):
        resources = []
    for resource in resources:
        relative = str(resource)
        path = resource_path(relative)
        checks.append(
            DiagnosticCheck(
                f"resource:{relative}",
                path.exists(),
                True,
                "present" if path.exists() else "missing",
            )
        )

    checks.extend(_validate_json_resources())
    checks.append(_check_data_dir(target_data_dir))

    required_ok = all(check.ok for check in checks if check.required)
    return DiagnosticReport(
        status="ok" if required_ok else "error",
        app_version=APP_VERSION,
        project_schema_version=PROJECT_SCHEMA_VERSION,
        frozen=bool(getattr(sys, "frozen", False)),
        data_dir=str(target_data_dir),
        checks=tuple(checks),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def write_health_file(data_dir: Path, payload: dict[str, object]) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "health.json"
    temporary = data_dir / f".health-{uuid4().hex}.tmp"
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=True, indent=2)
        file.write("\n")
    temporary.replace(target)
    return target


def _validate_json_resources() -> list[DiagnosticCheck]:
    """校验打包必需的 JSON 资源能被解析，且条数与预期一致。

    这里**只收录条数稳定的资源**。`Assets/.../DeviceLibrary.json` 是随
    Unity 序列化数据一起冻结的产物，条数固定 427，条数变了就是打包出错，
    值得报警。

    `app/data/equipment_catalog.json` **不在此列**：它是会持续增删设备类型
    的活目录（当前 551 条，由 53 条演进而来），写死条数只会随每次改目录
    过期，让 diagnostics 长期挂一盏红灯——而长期红灯会训练使用者忽略
    diagnostics，反而掩盖真实故障。该目录的正确性由
    `app/data/catalog_quality.py` 与 `tests/test_catalog_management.py`
    按角色必需字段校验，不靠条目计数。

    注意：条数不等于正确性。此检查只做「文件可解析 + 条数未意外漂移」，
    内容层面的校验见上方目录质量模块。
    """
    expected_counts = {
        "Assets/StreamingAssets/SoftwareSerializationData/Json/DeviceLibrary.json": 427,
    }
    checks: list[DiagnosticCheck] = []
    for relative, expected_count in expected_counts.items():
        path = resource_path(relative)
        if not path.exists():
            continue
        checks.append(_check_json_array(relative, path, expected_count=expected_count))

    # 活目录只校验「可解析且非空」，不钉条数：条数会随设备类型增删合法变化，
    # 但文件损坏或写空必须报出来。
    #
    # 关键：这里用的是**同一个检查名**，因此必须确保上面那份 expected_counts
    # 里不含该资源，否则会产生两条同名检查、互相矛盾（一条 FAIL 一条 OK），
    # 而按名索引的调用方只会看到其中一条。见
    # tests/test_diagnostics_api.py::test_live_catalog_check_does_not_pin_the_entry_count
    live_catalog = "app/data/equipment_catalog.json"
    assert live_catalog not in expected_counts, (
        f"{live_catalog} 同时出现在 expected_counts 与活目录检查中，"
        "会产生同名重复检查；请二选一"
    )
    if resource_path(live_catalog).exists():
        checks.append(_check_json_array(live_catalog, resource_path(live_catalog)))
    return checks


def _check_json_array(
    relative: str,
    path: Path,
    expected_count: int | None = None,
) -> DiagnosticCheck:
    """把一份 JSON 资源读成数组并生成检查结果。

    `expected_count` 为 `None` 时只校验「可解析、是数组、非空」——用于会持续
    增删条目的活目录；给出具体条数时额外比对，用于条数应冻结的打包产物。
    """
    name = f"content:{relative}"
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return DiagnosticCheck(name, False, True, f"{type(error).__name__}: {error}")

    if not isinstance(value, list):
        return DiagnosticCheck(
            name, False, True, f"expected a JSON array, got {type(value).__name__}"
        )
    if not value:
        return DiagnosticCheck(name, False, True, "entries=0; must not be empty")

    if expected_count is None:
        return DiagnosticCheck(name, True, True, f"entries={len(value)} (count not pinned)")
    return DiagnosticCheck(
        name,
        len(value) == expected_count,
        True,
        f"entries={len(value)}; expected={expected_count}",
    )


def _check_data_dir(data_dir: Path) -> DiagnosticCheck:
    probe = data_dir / f".write-probe-{uuid4().hex}"
    try:
        (data_dir / "projects").mkdir(parents=True, exist_ok=True)
        (data_dir / "backups").mkdir(parents=True, exist_ok=True)
        probe.write_text("ok\n", encoding="ascii")
        probe.unlink()
        return DiagnosticCheck("data_directory", True, True, "writable")
    except OSError as error:
        return DiagnosticCheck(
            "data_directory", False, True, f"{type(error).__name__}: {error}"
        )
