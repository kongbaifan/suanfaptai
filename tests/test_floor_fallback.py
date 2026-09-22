"""楼层兜底：未声明楼层的节点归属项目里**真实存在**的楼层，而不是凭空造一个

回归背景
--------
`app/building.py` 的 `room_floor_id` / `device_floor_id` / `pipe_floor_id` 在节点
**没有声明楼层**时兜底成字面量 `"default_floor"`，而 `validators/floors.py`
拿它与项目真实楼层比对 → **error** 级 `UNKNOWN_*_FLOOR`。

这条兜底在"项目一台楼层都没声明"时是对的：`normalized_floors` 会合成一个同
id 的楼层，兜底值与它一致。但项目**声明了楼层**时，这个 id 根本不存在，于是
每个没写楼层的节点白报一条 error。

实测：
- `MANIFOLD_FCU_SAMPLE` 的 6 根管子、`MANIFOLD_FCU_DUAL_WATER_SAMPLE` 的 12 根
  管子全部省略 `floor_id` → 该两个样例的 error 级 issue **全部**是
  `UNKNOWN_PIPE_FLOOR`（6 + 12 = 18 条）；
- 最小复现：楼层只有 `F1`，设备与管路都不写楼层 → 2 条 `UNKNOWN_DEVICE_FLOOR`
  + 1 条 `UNKNOWN_PIPE_FLOOR`，纯属记账问题。

用户路径也踩得到：`web/src/utils/project-editor.ts` 的 `addPipe()` 建管路时
**不设 `floor_id`**，`BuildingView.onAddPipe` 只在「楼层节点视图」里补楼层
（立管视图里只设 `pipe_type`）。所以"新增管路"在默认路径上就会变红。

判据（两个方向都要钉死）：
- **未声明**楼层 → 不报，且归属项目里真实存在的楼层（楼层汇总里不得出现
  `default_floor` 这种幽灵楼层）；
- **显式写了不存在的**楼层 → 照旧报 error——降噪不能把真错一起消掉。
"""

from collections import Counter

import pytest

from app.building import floor_base_summary
from app.project_validation import validate_project_data
from app.samples import SAMPLES
from app.schemas import SystemEvaluateRequest

FLOOR_CODES = ("UNKNOWN_ROOM_FLOOR", "UNKNOWN_DEVICE_FLOOR", "UNKNOWN_PIPE_FLOOR")


def _floor_codes(request: SystemEvaluateRequest) -> Counter:
    return Counter(
        issue.code
        for issue in validate_project_data(request).issues
        if issue.code in FLOOR_CODES
    )


def _named_floor_project() -> SystemEvaluateRequest:
    """有命名楼层、但设备与管路都不声明楼层的最小项目。"""
    return SystemEvaluateRequest(
        floors=[{"id": "F1", "name": "一层", "level": 1}], rooms=[
                {
                    "id": "r1",
                    "floor_id": "F1",
                    "name": "房间",
                    "device_ids": ["fcu1"],
                    "area_m2": 100,
                    "people": 10,
                }
            ], devices=[
                {"id": "fcu1", "type": "风机盘管", "name": "FP-204WA"},
                {"id": "ch1", "type": "冷源机组", "name": "SCWE-90H+B"},
            ], pipes=[
                {
                    "id": "p1",
                    "system": "chilled_water",
                    "pipe_type": "main_supply",
                    "length_m": 10,
                    "diameter_mm": 80,
                }
            ], connections=[
                {"from_id": "ch1", "to_id": "p1", "system": "chilled_water"},
                {"from_id": "p1", "to_id": "fcu1", "system": "chilled_water"},
            ]
    )


@pytest.mark.parametrize("sample_name", sorted(SAMPLES))
def test_samples_report_no_unknown_floor_issues(sample_name: str):
    """十个内置样例都不得报楼层不存在——包括两个 manifold 样例的 18 条假错"""
    codes = _floor_codes(SystemEvaluateRequest(**SAMPLES[sample_name]))
    assert codes == {}, (
        f"{sample_name} 仍有楼层错误：{dict(codes)}；"
        "未声明楼层的节点不该被兜底成一个不存在的楼层"
    )


def test_nodes_without_floor_inherit_a_declared_floor():
    """没写楼层的节点归到项目里真实存在的楼层，而不是幽灵楼层"""
    request = _named_floor_project()
    assert _floor_codes(request) == {}

    summary = floor_base_summary(request)
    assert set(summary) == {"F1"}, f"楼层汇总出现了非声明楼层：{set(summary)}"
    assert summary["F1"]["device_count"] == 2
    assert summary["F1"]["pipe_count"] == 1


def test_project_without_any_floor_uses_the_synthetic_floor():
    """项目一台楼层都没声明时，仍归到 normalized_floors 合成的那个楼层"""
    payload = _named_floor_project().model_dump()
    payload["floors"] = []
    payload["rooms"][0]["floor_id"] = None
    request = SystemEvaluateRequest(**payload)

    assert _floor_codes(request) == {}
    assert set(floor_base_summary(request)) == {"default_floor"}


def test_explicit_unknown_floor_still_reports():
    """显式写了不存在的楼层必须继续报——降噪不能把真错一起消掉"""
    payload = _named_floor_project().model_dump()
    payload["rooms"][0]["floor_id"] = "F9"
    payload["devices"][0]["floor_id"] = "F9"
    payload["pipes"][0]["floor_id"] = "F9"

    codes = _floor_codes(SystemEvaluateRequest(**payload))
    assert codes == Counter(
        {
            "UNKNOWN_ROOM_FLOOR": 1,
            "UNKNOWN_DEVICE_FLOOR": 1,
            "UNKNOWN_PIPE_FLOOR": 1,
        }
    ), f"显式写错的楼层被误放行：{dict(codes)}"


def test_explicit_unknown_riser_endpoint_floor_still_reports():
    """立管的 from/to 楼层写错也要报——它走的是另一条取值路径"""
    payload = _named_floor_project().model_dump()
    payload["pipes"][0].update(
        {"pipe_type": "riser_supply", "from_floor_id": "F1", "to_floor_id": "F9"}
    )

    codes = _floor_codes(SystemEvaluateRequest(**payload))
    assert codes.get("UNKNOWN_PIPE_FLOOR") == 1, (
        f"立管端点写了不存在的楼层却未报：{dict(codes)}"
    )
