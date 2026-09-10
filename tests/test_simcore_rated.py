"""固定参数解析 - 单元测试

覆盖设备身份桥接与四级降级：型号精确命中 -> 类型通配/中位型号 -> 内置兜底。
重点验证降级过程是否被显式标记，而不是让兜底值悄悄生效。
"""

from app.simcore.rated import RatedSpec, resolve_rated_spec, resolve_rated_spec_by_type
from app.unity_catalog import DeviceRole


class TestCatalogHit:
    """型号精确命中"""

    def test_chiller_reads_rated_values_from_catalog(self):
        """冷机额定参数应来自目录条目而非硬编码"""
        spec = resolve_rated_spec_by_type("冷源机组", "30XW-P0312P")

        assert spec.origin == "catalog"
        assert spec.model_name == "30XW-P0312P"
        assert spec.role is DeviceRole.CHILLER
        assert spec.capacity_kw == 303.5
        assert spec.cop == 5.65
        assert spec.iplv is None
        # chilled_flow_m3_h 经别名映射到通用的 flow_m3_h
        assert spec.flow_m3_h == 47.16
        assert spec.cooling_flow_m3_h == 58.68
        assert spec.degraded_fields == ()
        assert spec.is_degraded is False

    def test_legacy_model_alias_resolves_to_formal_catalog_model(self):
        spec = resolve_rated_spec_by_type("冷源机组", "LG340MCF")

        assert spec.origin == "catalog"
        assert spec.model_name == "30XW-P0312P"
        assert spec.capacity_kw == 303.5
        assert spec.degraded_fields == ()

    def test_different_models_give_different_parameters(self):
        """换型号必须换出不同的额定参数——这是教学联动的前提"""
        small = resolve_rated_spec_by_type("冷源机组", "30XW-P0312P")
        large = resolve_rated_spec_by_type("冷源机组", "30XW-P0452P")

        assert large.capacity_kw > small.capacity_kw
        assert large.flow_m3_h > small.flow_m3_h
        assert large.model_name != small.model_name

    def test_pump_reads_affinity_law_reference_point(self):
        """水泵相似定律的额定点来自目录"""
        spec = resolve_rated_spec_by_type("循环泵", "EC32")

        assert spec.origin == "catalog"
        assert spec.role is DeviceRole.PUMP
        assert spec.model_name == "TPE3 40-200"
        assert spec.flow_m3_h == 20.51
        assert spec.head_m == 14.9
        assert spec.power_kw == 1.119
        assert spec.efficiency == 0.687
        assert spec.variable_speed is True

    def test_terminal_water_flow_aliased(self):
        """末端的 water_flow_m3_h 映射到通用流量字段"""
        spec = resolve_rated_spec_by_type("风机盘管", "MCW200V")

        assert spec.origin == "catalog"
        assert spec.model_name == "FP-34WA"
        assert spec.capacity_kw == 2.2
        assert spec.airflow_m3_h == 340.0
        assert spec.flow_m3_h == 0.378
        assert spec.fan_power_kw == 0.026

    def test_heat_source_reads_heating_rated_values(self):
        spec = resolve_rated_spec_by_type("空气源热泵", "ASHP-240")

        assert spec.role is DeviceRole.HEAT_SOURCE
        assert spec.model_name == "CGK-100V3L-B"
        assert spec.capacity_kw == 34.5
        assert spec.flow_m3_h == 5.94
        assert spec.power_kw == 7.841
        assert spec.cop == 4.4
        assert spec.degraded_fields == ()


class TestDegradation:
    """降级链路"""

    def test_unknown_model_falls_back_to_type_default(self):
        """型号查不到时退到类型默认，并标记原因"""
        spec = resolve_rated_spec_by_type("冷源机组", "NO_SUCH_MODEL")

        assert spec.origin == "type_default"
        assert spec.role is DeviceRole.CHILLER
        assert spec.capacity_kw is not None
        assert "model_not_in_catalog" in spec.degraded_fields
        assert spec.is_degraded is True

    def test_type_default_is_deterministic(self):
        """同一类型的默认型号必须稳定，否则读数会无故漂移"""
        first = resolve_rated_spec_by_type("冷源机组", "NO_SUCH_MODEL")
        second = resolve_rated_spec_by_type("冷源机组", "ANOTHER_MISSING")

        assert first.model_name == second.model_name

    def test_formal_models_supply_deterministic_type_default(self):
        """没有通配条目时按正式型号确定类型默认"""
        spec = resolve_rated_spec_by_type("冷却塔", "WHATEVER")

        assert spec.origin == "type_default"
        assert spec.role is DeviceRole.COOLING_TOWER
        assert spec.model_name == "S15E-1285-06JN"
        assert spec.approach_c is not None

    def test_unknown_type_falls_back_to_builtin(self):
        """类型也查不到时使用内置兜底，并记录原因"""
        spec = resolve_rated_spec_by_type("根本不存在的设备类型", None)

        assert spec.origin == "builtin_fallback"
        assert "catalog_type_missing" in spec.degraded_fields
        assert spec.is_degraded is True

    def test_unity_identity_not_found_is_marked(self):
        """Unity 设备身份查不到时不静默返回魔数"""
        spec = resolve_rated_spec(-1, -1)

        assert spec.origin == "builtin_fallback"
        assert "unity_device_not_found" in spec.degraded_fields

    def test_invalid_unity_identity_does_not_raise(self):
        """非数字设备身份应降级而不是抛异常"""
        spec = resolve_rated_spec("not-a-number", "x")

        assert isinstance(spec, RatedSpec)
        assert spec.origin == "builtin_fallback"
