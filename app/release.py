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
        raise ValueError("release manifest must contain a JSON object")
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
    expected_counts = {
        "app/data/equipment_catalog.json": 53,
        "Assets/StreamingAssets/SoftwareSerializationData/Json/DeviceLibrary.json": 427,
    }
    checks: list[DiagnosticCheck] = []
    for relative, expected_count in expected_counts.items():
        path = resource_path(relative)
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as file:
                value = json.load(file)
            actual_count = len(value) if isinstance(value, list) else -1
            checks.append(
                DiagnosticCheck(
                    f"content:{relative}",
                    actual_count == expected_count,
                    True,
                    f"entries={actual_count}; expected={expected_count}",
                )
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            checks.append(
                DiagnosticCheck(
                    f"content:{relative}",
                    False,
                    True,
                    f"{type(error).__name__}: {error}",
                )
            )
    return checks


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
