"""Unity 设备目录映射集成示例

演示如何使用 unity_catalog 模块。
"""

from app.unity_catalog import (
    get_device_spec,
    get_all_device_specs,
    find_equipment_spec,
)


def example_usage():
    """使用示例"""

    print("=" * 60)
    print("Unity Device Catalog Integration Examples")
    print("=" * 60)
    print()

    # 1. 通过 (DeviceID, ModelID) 查询
    print("1. Query by (DeviceID, ModelID):")
    spec = get_device_spec(132, 1)
    if spec:
        print(f"   Device: {spec.device_type} - {spec.device_name}")
        print(f"   Role: {spec.role}")
        print(f"   System: {spec.system_type}")
        print(f"   Behaviors: {spec.solver_behaviors}")
        print(f"   Source: {spec.source}")
        if spec.issues:
            print(f"   Issues: {spec.issues[0]}")
    print()

    # 2. 通过 (type, name) 查询（向后兼容）
    print("2. Query by (type, name) - backward compatibility:")
    spec = find_equipment_spec("循环泵", "EC40")
    if spec:
        print(f"   Device: {spec.device_type} - {spec.device_name}")
        print(f"   Role: {spec.role}")
        print(f"   Issues: {len(spec.issues)} issue(s)")
    print()

    # 3. 铜管浮点名称处理
    print("3. Copper pipe with float name:")
    specs = get_all_device_specs()
    copper_pipes = [s for s in specs if s.device_type == "定位铜管"]
    if copper_pipes:
        sample = copper_pipes[0]
        print(f"   Device: {sample.device_type} - {sample.device_name} (type: {type(sample.device_name).__name__})")
        print(f"   OD: {sample.geometric.od} mm")
    print()

    # 4. 几何参数解析
    print("4. Geometric parameter parsing:")
    rect_pipe = find_equipment_spec("定位方管", "400x250")
    if rect_pipe:
        print(f"   Device: {rect_pipe.device_name}")
        print(f"   Width: {rect_pipe.geometric.width} mm")
        print(f"   Height: {rect_pipe.geometric.height} mm")
    print()

    # 5. 角色分类统计
    print("5. Role distribution:")
    from collections import Counter
    role_dist = Counter(s.role.value if s.role else "unknown" for s in specs)
    for role, count in role_dist.most_common(5):
        print(f"   {role:25s}: {count:3d}")
    print()

    # 6. DeviceID=11 的三类型分段
    print("6. DeviceID=11 segmentation:")
    id_11_specs = [s for s in specs if s.device_id == 11]
    id_11_types = {s.device_type for s in id_11_specs}
    print(f"   Types: {', '.join(sorted(id_11_types))}")
    print(f"   Total models: {len(id_11_specs)}")
    print()

    # 7. Unresolved 参数统计
    print("7. Unresolved parameters:")
    unresolved = [s for s in specs if s.source == "unresolved"]
    unresolved_types = Counter(s.device_type for s in unresolved)
    for device_type, count in unresolved_types.most_common():
        print(f"   {device_type:20s}: {count:3d}")
    print()

    print("=" * 60)


if __name__ == "__main__":
    example_usage()
