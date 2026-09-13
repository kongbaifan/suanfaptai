"""几何参数解析器

从 Unity DeviceName 派生几何量（DN、尺寸、铜管 OD、异径对）。
"""

import re


def parse_dn(device_name: str) -> float | None:
    """解析管道 DN 规格

    支持格式：DN{n}、DN {n}、{n}

    Args:
        device_name: 设备名称

    Returns:
        DN 值（mm），解析失败返回 None

    Examples:
        >>> parse_dn("DN200")
        200.0
        >>> parse_dn("DN 150")
        150.0
        >>> parse_dn("200")  # 纯数字
        200.0
    """
    # DN200 或 DN 200
    match = re.match(r'^DN\s*(\d+(?:\.\d+)?)$', device_name, re.IGNORECASE)
    if match:
        return float(match.group(1))

    # 纯数字（铜管 OD）
    try:
        return float(device_name)
    except ValueError:
        return None


def parse_rect_size(device_name: str) -> tuple[float, float] | None:
    """解析矩形尺寸

    支持格式：{W}x{H}、{W}×{H}（ASCII x 或全角 ×）

    Args:
        device_name: 设备名称

    Returns:
        (宽, 高) 元组（mm），解析失败返回 None

    Examples:
        >>> parse_rect_size("400x250")
        (400.0, 250.0)
        >>> parse_rect_size("600×400")
        (600.0, 400.0)
    """
    # 支持 ASCII x 和全角 ×
    match = re.match(r'^(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)$', device_name, re.IGNORECASE)
    if match:
        w = float(match.group(1))
        h = float(match.group(2))
        return (w, h)
    return None


def parse_reducer_pair(device_name: str) -> tuple[float, float] | None:
    """解析异径管规格对

    支持格式：{a}-{b}

    Args:
        device_name: 设备名称（如 "41.3-38.1"）

    Returns:
        (大端, 小端) 元组（mm），解析失败返回 None

    Examples:
        >>> parse_reducer_pair("41.3-38.1")
        (41.3, 38.1)
        >>> parse_reducer_pair("50-40")
        (50.0, 40.0)
    """
    match = re.match(r'^(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)$', device_name)
    if match:
        a = float(match.group(1))
        b = float(match.group(2))
        return (a, b)
    return None


def parse_copper_pipe_od(device_name: str) -> float | None:
    """解析铜管外径

    铜管 DeviceName 在 JSON 中为数字字面量，Python 读出为 float/int。

    Args:
        device_name: 设备名称（数字或字符串）

    Returns:
        外径（mm），解析失败返回 None

    Examples:
        >>> parse_copper_pipe_od("15.88")
        15.88
        >>> parse_copper_pipe_od("22.23")
        22.23
    """
    try:
        return float(device_name)
    except (ValueError, TypeError):
        return None


def parse_manifold_dn(device_name: str) -> float | None:
    """解析分集水器 DN 规格

    分集水器名称格式通常为 DN{n} 或纯数字。

    Args:
        device_name: 设备名称

    Returns:
        DN 值（mm），解析失败返回 None

    Examples:
        >>> parse_manifold_dn("DN100")
        100.0
        >>> parse_manifold_dn("150")
        150.0
    """
    return parse_dn(device_name)
