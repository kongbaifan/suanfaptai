"""设备参数字段名的中文标签。

内部存储与计算始终使用英文 key；仅在 GET 响应出口把字段名翻译成中文，
让前端/调用方直接收到中文字段名。

同一个 key 在 `equipment_knowledge.json` 的 `key_parameters.name` 里可能用更
具体的说法（例如 `capacity_kw` 在风机盘管知识条目中叫「额定制冷量」，此处统一
叫「额定容量」）。两处刻意不强行对齐：知识条目按设备类型给出语义化名称，本表
是跨设备类型通用的字段名。前端取值必须用本表的名字。
"""

from __future__ import annotations

from typing import Any, Mapping

FIELD_LABELS_ZH: dict[str, str] = {
    # 标识字段
    "type": "设备类型",
    "name": "型号",
    "brand": "品牌",
    "series": "系列",
    "role": "设备角色",
    "systems": "适用系统",
    "aliases": "兼容旧型号",
    "source_url": "厂家资料",
    "derived_fields": "派生字段",
    "refrigerant": "制冷剂",
    "configurable_series": "按项目选型",
    "placement_only": "仅摆放",
    "unity_entries": "Unity摆放信息",
    # 性能参数
    "capacity_kw": "额定容量",
    "cop": "性能系数",
    "iplv": "综合部分负荷性能系数",
    "power_kw": "输入功率",
    "flow_m3_h": "额定流量",
    "head_m": "额定扬程",
    "pressure_drop_pa": "压降",
    "airflow_m3_h": "额定风量",
    "chilled_flow_m3_h": "冷冻水流量",
    "cooling_flow_m3_h": "冷却水流量",
    "chilled_pressure_drop_kpa": "冷冻水压降",
    "cooling_pressure_drop_kpa": "冷却水压降",
    "efficiency": "效率",
    "rpm": "转速",
    "npsh_m": "必需汽蚀余量",
    "approach_c": "逼近度",
    "fan_power_kw": "风机功率",
    "noise_db": "噪声",
    "water_flow_m3_h": "水流量",
    "water_pressure_drop_kpa": "水侧压降",
    "external_static_pressure_pa": "机外静压",
    "local_resistance_coefficient": "局部阻力系数",
    "air_pressure_drop_pa": "风侧压降",
    "recommended_airflow_m3_h": "推荐风量",
    "unit_price": "单价",
    "weight_kg": "重量",
    "heating_capacity_kw": "额定制热量",
    "heating_power_kw": "制热输入功率",
    "cooling_capacity_kw": "额定制冷量",
    "cooling_power_kw": "制冷输入功率",
    "eer": "EER",
    "source_flow_m3_h": "源侧流量",
    "nominal_capacity_ton": "名义冷吨",
    "airflow_min_m3_h": "最小风量",
    "airflow_max_m3_h": "最大风量",
    "airflow_range_cfm": "系列风量范围",
    "motor_efficiency": "电机效率",
    "noise_range_db_a": "噪声范围",
    "cooling_outdoor_dry_bulb_temp_c": "制冷室外干球温度",
    "heating_outdoor_dry_bulb_temp_c": "制热室外干球温度",
    "cooling_evaporator_inlet_temp_c": "制冷蒸发器进水温度",
    "cooling_evaporator_outlet_temp_c": "制冷蒸发器出水温度",
    "cooling_condenser_inlet_temp_c": "制冷冷凝器进水温度",
    "cooling_condenser_outlet_temp_c": "制冷冷凝器出水温度",
    "heating_evaporator_inlet_temp_c": "制热蒸发器进水温度",
    "heating_evaporator_outlet_temp_c": "制热蒸发器出水温度",
    "heating_condenser_inlet_temp_c": "制热冷凝器进水温度",
    "heating_condenser_outlet_temp_c": "制热冷凝器出水温度",
    "variable_speed": "变频能力",
    "water_cooled": "水冷",
}


def keys_to_zh(item: Mapping[str, Any]) -> dict[str, Any]:
    """把字典的英文字段名翻译成中文，未知名段原样保留。"""
    return {FIELD_LABELS_ZH.get(str(key), str(key)): value for key, value in item.items()}
