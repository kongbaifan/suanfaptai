"""Hardy Cross 流量求解器 - 阻力系数计算

基于 Hazen-Williams 或 Darcy-Weisbach 公式计算管道阻力系数。
"""

from app.hardy_cross.models import PipeEdge, SolverConfig


def calculate_resistance_coefficient(edge: PipeEdge, config: SolverConfig) -> float:
    """计算管道阻力系数 K

    使用 Hazen-Williams 公式（适用于水系统）：
    ΔP = K × Q^1.852

    其中：
    K = 10.67 × L / (C^1.852 × D^4.87)

    Args:
        edge: 管道边
        config: 求解器配置

    Returns:
        阻力系数 K（Pa/(m³/h)^1.852）
    """
    L = edge.length_m
    D = edge.diameter_mm / 1000  # 转换为米
    C = config.hazen_williams_C

    if D <= 0 or L <= 0:
        return 0.0

    # Hazen-Williams 公式
    # K = 10.67 × L / (C^1.852 × D^4.87)
    K = 10.67 * L / (C**1.852 * D**4.87)

    return K


def calculate_pressure_drop(edge: PipeEdge, flow_m3_h: float, config: SolverConfig) -> float:
    """计算管道压降

    Args:
        edge: 管道边
        flow_m3_h: 流量（m³/h）
        config: 求解器配置

    Returns:
        压降（Pa，正值表示压力损失）
    """
    if edge.resistance_coeff == 0.0:
        edge.resistance_coeff = calculate_resistance_coefficient(edge, config)

    K = edge.resistance_coeff
    n = config.flow_exponent

    # ΔP = K × |Q|^(n-1) × Q （保留符号）
    if abs(flow_m3_h) < 1e-9:
        return 0.0

    return K * abs(flow_m3_h)**(n - 1) * flow_m3_h


def calculate_resistance_derivative(
    edge: PipeEdge,
    flow_m3_h: float,
    config: SolverConfig
) -> float:
    """计算阻力对流量的导数（用于 Hardy Cross 修正）

    d(ΔP)/dQ = K × n × |Q|^(n-1)

    Args:
        edge: 管道边
        flow_m3_h: 流量（m³/h）
        config: 求解器配置

    Returns:
        导数值（Pa/(m³/h)）
    """
    if edge.resistance_coeff == 0.0:
        edge.resistance_coeff = calculate_resistance_coefficient(edge, config)

    K = edge.resistance_coeff
    n = config.flow_exponent

    # d(ΔP)/dQ = K × n × |Q|^(n-1)
    if abs(flow_m3_h) < 1e-9:
        # 在零流量处使用小值避免除零
        return K * n * 1e-9**(n - 1)

    return K * n * abs(flow_m3_h)**(n - 1)


def initialize_edge_resistances(model, config: SolverConfig):
    """预计算所有边的阻力系数（优化性能）

    Args:
        model: 管网模型
        config: 求解器配置
    """
    for edge in model.edges.values():
        edge.resistance_coeff = calculate_resistance_coefficient(edge, config)
