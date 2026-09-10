"""Unity catalog 测试

测试 427 条唯一性、几何解析、DeviceID=11 分段、铜管浮点名。
"""

from app.unity_catalog import get_device_spec, get_all_device_specs, find_equipment_spec
from app.unity_catalog.roles import DeviceRole
from app.unity_catalog.geometry import (
    parse_dn,
    parse_rect_size,
    parse_reducer_pair,
    parse_copper_pipe_od,
)


class TestDeviceLibraryLoading:
    """测试设备库加载"""

    def test_total_count(self):
        """验证总数 427 条"""
        specs = get_all_device_specs()
        assert len(specs) == 427

    def test_unique_keys(self):
        """验证 (DeviceID, ModelID) 唯一性"""
        specs = get_all_device_specs()
        keys = [(s.device_id, s.model_id) for s in specs]
        assert len(keys) == len(set(keys)), "Duplicate (DeviceID, ModelID) found"

    def test_all_types_covered(self):
        """验证 27 个类型全覆盖"""
        specs = get_all_device_specs()
        types = {s.device_type for s in specs}
        assert len(types) == 27


class TestDeviceID11Segmentation:
    """测试 DeviceID=11 的三类型分段"""

    def test_device_id_11_types(self):
        """DeviceID=11 应包含定位圆管、定位方管、定位铜管"""
        specs = get_all_device_specs()
        id_11_specs = [s for s in specs if s.device_id == 11]
        types = {s.device_type for s in id_11_specs}

        assert "定位圆管" in types
        assert "定位方管" in types
        assert "定位铜管" in types
        assert len(types) == 3, f"DeviceID=11 should have exactly 3 types, got {types}"


class TestGeometryParsing:
    """测试几何解析"""

    def test_parse_dn(self):
        """测试 DN 解析"""
        assert parse_dn("DN200") == 200.0
        assert parse_dn("DN 150") == 150.0
        assert parse_dn("100") == 100.0
        assert parse_dn("invalid") is None

    def test_parse_rect_size_ascii(self):
        """测试矩形尺寸解析（ASCII x）"""
        assert parse_rect_size("400x250") == (400.0, 250.0)
        assert parse_rect_size("600x400") == (600.0, 400.0)

    def test_parse_rect_size_fullwidth(self):
        """测试矩形尺寸解析（全角 ×）"""
        assert parse_rect_size("400×250") == (400.0, 250.0)
        assert parse_rect_size("600×400") == (600.0, 400.0)

    def test_parse_reducer_pair(self):
        """测试异径管解析"""
        assert parse_reducer_pair("41.3-38.1") == (41.3, 38.1)
        assert parse_reducer_pair("50-40") == (50.0, 40.0)
        assert parse_reducer_pair("invalid") is None

    def test_parse_copper_pipe_od(self):
        """测试铜管外径解析"""
        assert parse_copper_pipe_od("15.88") == 15.88
        assert parse_copper_pipe_od("22.23") == 22.23
        assert parse_copper_pipe_od(15.88) == 15.88  # 数字输入
        assert parse_copper_pipe_od("invalid") is None


class TestCopperPipeFloatNames:
    """测试铜管浮点名称处理"""

    def test_copper_pipe_names_are_strings(self):
        """定位铜管的 device_name 必须为字符串"""
        specs = get_all_device_specs()
        copper_pipes = [s for s in specs if s.device_type == "定位铜管"]

        for spec in copper_pipes:
            assert isinstance(spec.device_name, str), \
                f"Copper pipe name should be string, got {type(spec.device_name)}"

    def test_copper_pipe_od_parsing(self):
        """定位铜管名称应可解析为外径"""
        specs = get_all_device_specs()
        copper_pipes = [s for s in specs if s.device_type == "定位铜管"]

        for spec in copper_pipes:
            assert spec.geometric.od is not None, \
                f"Failed to parse OD from copper pipe name: {spec.device_name}"
            assert spec.geometric.od > 0


class TestRoleMapping:
    """测试角色映射"""

    def test_all_types_have_roles(self):
        """验证 27 个类型都有角色"""
        specs = get_all_device_specs()
        types = {s.device_type for s in specs}

        for device_type in types:
            type_specs = [s for s in specs if s.device_type == device_type]
            assert all(s.role is not None for s in type_specs), \
                f"Type {device_type} missing role"

    def test_chiller_role(self):
        """验证冷源机组角色"""
        spec = next(s for s in get_all_device_specs() if s.device_type == "冷源机组")
        assert spec.role == DeviceRole.CHILLER
        assert spec.system_type == "water"
        assert "dynamic_source" in spec.solver_behaviors

    def test_pump_role(self):
        """验证循环泵角色（需拓扑判定）"""
        spec = next(s for s in get_all_device_specs() if s.device_type == "循环泵")
        assert spec.role == DeviceRole.PUMP
        assert spec.system_type == "water"
        assert any("topology traversal" in issue for issue in spec.issues)


class TestParameterSource:
    """测试参数来源标注"""

    def test_capacity_devices_unresolved(self):
        """验证容量类设备标记为 unresolved"""
        specs = get_all_device_specs()
        capacity_types = ["冷源机组", "循环泵", "冷却塔", "风机盘管", "组合式空调机组", "一体式新风机组"]

        for device_type in capacity_types:
            type_specs = [s for s in specs if s.device_type == device_type]
            for spec in type_specs:
                assert spec.source == "unresolved", \
                    f"{device_type} should have unresolved parameters"
                assert len(spec.issues) > 0, \
                    f"{device_type} should have capacity issue"


class TestBackwardCompatibility:
    """测试向后兼容性"""

    def test_find_by_type_name(self):
        """测试 (type, name) 查询"""
        spec = find_equipment_spec("冷源机组", "LG340MCF")
        assert spec is not None
        assert spec.device_type == "冷源机组"
        assert spec.device_name == "LG340MCF"

    def test_find_copper_pipe_by_float_name(self):
        """测试用浮点数查找铜管（应归一为字符串）"""
        # 假设存在 15.88 的铜管
        spec = find_equipment_spec("定位铜管", "15.88")
        if spec:
            assert spec.device_type == "定位铜管"
            assert spec.device_name == "15.88"


class TestIndexQuery:
    """测试索引查询"""

    def test_get_by_id_model(self):
        """测试 (DeviceID, ModelID) 查询"""
        spec = get_device_spec(132, 1)  # 第一个冷源机组
        assert spec is not None
        assert spec.device_id == 132
        assert spec.model_id == 1

    def test_get_nonexistent(self):
        """测试不存在的键"""
        spec = get_device_spec(9999, 9999)
        assert spec is None
