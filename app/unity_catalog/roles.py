"""设备角色定义与分类

定义 Unity 27 类设备的角色、语义和求解行为。
"""

from enum import Enum


class DeviceRole(str, Enum):
    """设备角色分类"""

    # 核心动态设备
    CHILLER = "chiller"  # 冷源
    HEAT_SOURCE = "heat_source"  # 热源/热泵
    PUMP = "pump"  # 循环泵（冷冻/冷却需拓扑判定）
    COOLING_TOWER = "cooling_tower"  # 冷却塔
    FCU = "fcu"  # 风机盘管
    AHU = "ahu"  # 组合式空调机组
    FRESH_AIR_UNIT = "fresh_air_unit"  # 新风机组（冷冻水盘管，100% 室外新风）

    # 控制与阻力
    WATER_VALVE = "water_valve"  # 水阀（球阀、截止阀、蝶阀、闸阀、止回阀）
    AIR_VALVE = "air_valve"  # 风阀
    FIRE_DAMPER = "fire_damper"  # 防火阀
    MANIFOLD = "manifold"  # 分集水器
    AIR_OUTLET = "air_outlet"  # 风口（送风口/回风口，均为风系统拓扑节点）

    # 拓扑连接
    PIPE_STRAIGHT = "pipe_straight"  # 直管（圆/定位圆/定位方/定位铜）
    PIPE_TEE = "pipe_tee"  # 三通（圆/方）
    PIPE_CROSS = "pipe_cross"  # 四通（圆/方）
    PIPE_REDUCER = "pipe_reducer"  # 异径管（铜异径管）
    PIPE_BRANCH = "pipe_branch"  # 分歧管

    # 测量
    MEASUREMENT = "measurement"  # 测量件（温度、压力、流量）

    # 待细分
    PUMP_CHILLED_WATER = "pump_chilled_water"  # 冷冻水泵（需拓扑判定）
    PUMP_COOLING_WATER = "pump_cooling_water"  # 冷却水泵（需拓扑判定）


class SystemType(str, Enum):
    """支持系统类型"""
    WATER = "water"  # 水系统
    AIR = "air"  # 风系统
    BOTH = "both"  # 水风混合


class SolverBehavior(str, Enum):
    """求解行为"""
    DYNAMIC_SOURCE = "dynamic_source"  # 动态源（产生能量）
    DYNAMIC_LOAD = "dynamic_load"  # 动态负荷（消耗能量）
    CONTROL_RESISTANCE = "control_resistance"  # 控制阻力
    TOPOLOGY_JUNCTION = "topology_junction"  # 拓扑节点
    TOPOLOGY_SEGMENT = "topology_segment"  # 拓扑线段
    MEASUREMENT_POINT = "measurement_point"  # 测量点


# Unity DeviceType -> Role 映射
DEVICE_TYPE_TO_ROLE: dict[str, DeviceRole] = {
    # 动态设备
    "冷源机组": DeviceRole.CHILLER,
    "空气源热泵": DeviceRole.HEAT_SOURCE,
    "水源热泵": DeviceRole.HEAT_SOURCE,
    "地源热泵机组": DeviceRole.HEAT_SOURCE,
    "循环泵": DeviceRole.PUMP,
    "冷却塔": DeviceRole.COOLING_TOWER,
    "风机盘管": DeviceRole.FCU,
    "组合式空调机组": DeviceRole.AHU,
    "新风机组": DeviceRole.FRESH_AIR_UNIT,

    # 控制与阻力
    "球阀": DeviceRole.WATER_VALVE,
    "截止阀": DeviceRole.WATER_VALVE,
    "蝶阀": DeviceRole.WATER_VALVE,
    "闸阀": DeviceRole.WATER_VALVE,
    "止回阀": DeviceRole.WATER_VALVE,
    "风阀": DeviceRole.AIR_VALVE,
    "防火阀": DeviceRole.FIRE_DAMPER,
    "分水器": DeviceRole.MANIFOLD,
    "集水器": DeviceRole.MANIFOLD,
    "送风口": DeviceRole.AIR_OUTLET,
    "回风口": DeviceRole.AIR_OUTLET,

    # 拓扑
    "圆直管": DeviceRole.PIPE_STRAIGHT,
    "定位圆管": DeviceRole.PIPE_STRAIGHT,
    "定位方管": DeviceRole.PIPE_STRAIGHT,
    "定位铜管": DeviceRole.PIPE_STRAIGHT,
    "圆三通": DeviceRole.PIPE_TEE,
    "方三通": DeviceRole.PIPE_TEE,
    "圆四通": DeviceRole.PIPE_CROSS,
    "方四通": DeviceRole.PIPE_CROSS,
    "铜异径管": DeviceRole.PIPE_REDUCER,
    "分歧管": DeviceRole.PIPE_BRANCH,

    # 测量
    "测量件": DeviceRole.MEASUREMENT,
}


# Role -> System 映射
ROLE_TO_SYSTEM: dict[DeviceRole, SystemType] = {
    DeviceRole.CHILLER: SystemType.WATER,
    DeviceRole.HEAT_SOURCE: SystemType.WATER,
    DeviceRole.PUMP: SystemType.WATER,
    DeviceRole.PUMP_CHILLED_WATER: SystemType.WATER,
    DeviceRole.PUMP_COOLING_WATER: SystemType.WATER,
    DeviceRole.COOLING_TOWER: SystemType.WATER,
    DeviceRole.FCU: SystemType.BOTH,
    DeviceRole.AHU: SystemType.BOTH,
    DeviceRole.FRESH_AIR_UNIT: SystemType.AIR,
    DeviceRole.WATER_VALVE: SystemType.WATER,
    DeviceRole.AIR_VALVE: SystemType.AIR,
    DeviceRole.FIRE_DAMPER: SystemType.AIR,
    DeviceRole.MANIFOLD: SystemType.WATER,
    DeviceRole.AIR_OUTLET: SystemType.AIR,
    DeviceRole.PIPE_STRAIGHT: SystemType.WATER,
    DeviceRole.PIPE_TEE: SystemType.WATER,
    DeviceRole.PIPE_CROSS: SystemType.WATER,
    DeviceRole.PIPE_REDUCER: SystemType.WATER,
    DeviceRole.PIPE_BRANCH: SystemType.WATER,
    DeviceRole.MEASUREMENT: SystemType.BOTH,
}


# Role -> Solver Behavior 映射
ROLE_TO_BEHAVIOR: dict[DeviceRole, set[SolverBehavior]] = {
    DeviceRole.CHILLER: {SolverBehavior.DYNAMIC_SOURCE},
    DeviceRole.HEAT_SOURCE: {SolverBehavior.DYNAMIC_SOURCE},
    DeviceRole.PUMP: {SolverBehavior.DYNAMIC_SOURCE},
    DeviceRole.PUMP_CHILLED_WATER: {SolverBehavior.DYNAMIC_SOURCE},
    DeviceRole.PUMP_COOLING_WATER: {SolverBehavior.DYNAMIC_SOURCE},
    DeviceRole.COOLING_TOWER: {SolverBehavior.DYNAMIC_LOAD},
    DeviceRole.FCU: {SolverBehavior.DYNAMIC_LOAD},
    DeviceRole.AHU: {SolverBehavior.DYNAMIC_LOAD},
    DeviceRole.FRESH_AIR_UNIT: {SolverBehavior.DYNAMIC_LOAD},
    DeviceRole.WATER_VALVE: {SolverBehavior.CONTROL_RESISTANCE},
    DeviceRole.AIR_VALVE: {SolverBehavior.CONTROL_RESISTANCE},
    DeviceRole.FIRE_DAMPER: {SolverBehavior.CONTROL_RESISTANCE},
    DeviceRole.MANIFOLD: {SolverBehavior.TOPOLOGY_JUNCTION},
    DeviceRole.AIR_OUTLET: {SolverBehavior.TOPOLOGY_JUNCTION},
    DeviceRole.PIPE_STRAIGHT: {SolverBehavior.TOPOLOGY_SEGMENT},
    DeviceRole.PIPE_TEE: {SolverBehavior.TOPOLOGY_JUNCTION},
    DeviceRole.PIPE_CROSS: {SolverBehavior.TOPOLOGY_JUNCTION},
    DeviceRole.PIPE_REDUCER: {SolverBehavior.TOPOLOGY_SEGMENT},
    DeviceRole.PIPE_BRANCH: {SolverBehavior.TOPOLOGY_JUNCTION},
    DeviceRole.MEASUREMENT: {SolverBehavior.MEASUREMENT_POINT},
}


def get_device_role(device_type: str) -> DeviceRole | None:
    """根据 Unity DeviceType 获取设备角色

    Args:
        device_type: Unity 设备类型（如"冷源机组"）

    Returns:
        设备角色，未知类型返回 None
    """
    # 历史类型名归一，避免改名前的项目/端口布局解析不到角色。
    from app.data.equipment import resolve_device_type

    return DEVICE_TYPE_TO_ROLE.get(resolve_device_type(device_type))


def get_system_type(role: DeviceRole) -> SystemType:
    """获取设备角色所属系统"""
    return ROLE_TO_SYSTEM[role]


def get_solver_behaviors(role: DeviceRole) -> set[SolverBehavior]:
    """获取设备角色的求解行为"""
    return ROLE_TO_BEHAVIOR[role]
