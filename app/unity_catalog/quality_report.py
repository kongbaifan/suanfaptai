"""目录质量报告生成器

生成设备目录的参数覆盖率、来源分布和 unresolved 位置报告。
"""

from collections import Counter

from .catalog import get_all_device_specs


def generate_quality_report() -> str:
    """生成目录质量报告

    Returns:
        格式化的质量报告文本
    """
    specs = get_all_device_specs()

    # 统计基础数据
    total_count = len(specs)
    type_count = len({s.device_type for s in specs})

    # 参数来源分布
    source_dist = Counter(s.source for s in specs)

    # 按类型分组的来源统计
    type_source: dict[str, Counter] = {}
    for spec in specs:
        if spec.device_type not in type_source:
            type_source[spec.device_type] = Counter()
        type_source[spec.device_type][spec.source] += 1

    # unresolved 位置
    unresolved_specs = [s for s in specs if s.source == "unresolved"]
    unresolved_by_type = Counter(s.device_type for s in unresolved_specs)

    # 问题统计
    specs_with_issues = [s for s in specs if s.issues]
    issue_types: Counter[str] = Counter()
    for spec in specs_with_issues:
        for issue in spec.issues:
            if "Capacity" in issue:
                issue_types["capacity_unresolved"] += 1
            elif "topology traversal" in issue:
                issue_types["pump_role_pending"] += 1
            else:
                issue_types["other"] += 1

    # 几何解析覆盖率
    geom_parsed = sum(1 for s in specs if any([
        s.geometric.dn is not None,
        s.geometric.width is not None,
        s.geometric.od is not None,
        s.geometric.reducer_large is not None,
    ]))

    # 生成报告
    lines = [
        "=" * 60,
        "Unity Device Catalog Quality Report",
        "=" * 60,
        "",
        "## Basic Statistics",
        f"Total Devices: {total_count}",
        f"Device Types: {type_count}",
        "",
        "## Parameter Source Distribution",
        f"  measured:            {source_dist['measured']:3d} ({source_dist['measured']/total_count*100:5.1f}%)",
        f"  engineering_default: {source_dist['engineering_default']:3d} ({source_dist['engineering_default']/total_count*100:5.1f}%)",
        f"  derived_from_name:   {source_dist['derived_from_name']:3d} ({source_dist['derived_from_name']/total_count*100:5.1f}%)",
        f"  unresolved:          {source_dist['unresolved']:3d} ({source_dist['unresolved']/total_count*100:5.1f}%)",
        "",
        "## Unresolved by Type",
    ]

    for device_type, count in unresolved_by_type.most_common():
        lines.append(f"  {device_type:20s}: {count:3d}")

    lines.extend([
        "",
        "## Issue Summary",
        f"  Capacity/Performance Unresolved: {issue_types['capacity_unresolved']}",
        f"  Pump Role Pending (Topology):    {issue_types['pump_role_pending']}",
        f"  Other Issues:                    {issue_types['other']}",
        "",
        "## Geometry Parsing Coverage",
        f"  Devices with Parsed Geometry: {geom_parsed}/{total_count} ({geom_parsed/total_count*100:5.1f}%)",
        "",
        "## Type-Level Source Breakdown",
    ])

    for device_type in sorted(type_source.keys()):
        dist = type_source[device_type]
        type_total = sum(dist.values())
        lines.append(f"  {device_type}:")
        for source in ["measured", "engineering_default", "derived_from_name", "unresolved"]:
            if dist[source] > 0:
                lines.append(f"    {source:20s}: {dist[source]:3d}/{type_total:3d}")

    lines.append("=" * 60)

    return "\n".join(lines)


if __name__ == "__main__":
    print(generate_quality_report())
