"""全局测试隔离。

历史问题：多数测试直接用 `TestClient(app)` 创建/删除项目，写的是真实的
`app/data/projects/` 与 `app/data/realtime_history/`。一旦测试中途失败或
fixture 未覆盖，就会在用户的项目列表里留下 `pytest_realtime` 之类的垃圾条目。

这里用 autouse fixture 把两个目录整体重定向到 pytest 的临时目录，
任何测试都不可能再写到真实数据。个别测试若自带 monkeypatch 会更具体，
仍然覆盖本 fixture，行为不变。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from app import realtime
from app.data import equipment, project_store


@pytest.fixture(autouse=True)
def isolate_persistent_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(project_store, "PROJECT_DIR", tmp_path / "projects")
    monkeypatch.setattr(realtime, "HISTORY_DIR", tmp_path / "realtime_history")


@pytest.fixture
def isolated_equipment_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Catalogue mutation tests use a copy, including when a regression fails."""
    target = tmp_path / "equipment_catalog.json"
    target.write_bytes(equipment.CATALOG_PATH.read_bytes())
    monkeypatch.setattr(equipment, "CATALOG_PATH", target)
    equipment.load_equipment_catalog.cache_clear()
    try:
        yield target
    finally:
        equipment.load_equipment_catalog.cache_clear()
