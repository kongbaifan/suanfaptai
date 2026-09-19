import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { DEVICE_TYPE_OPTIONS, ROOM_TERMINAL_DEVICE_TYPES } from "./project-editor";

/** 读后端设备目录，取全部 `type` 值（嵌套结构统一遍历）。 */
function catalogTypes(): Set<string> {
  const path = resolve(__dirname, "../../../app/data/equipment_catalog.json");
  const data = JSON.parse(readFileSync(path, "utf-8"));

  const found = new Set<string>();
  const walk = (node: unknown): void => {
    if (Array.isArray(node)) {
      node.forEach(walk);
      return;
    }
    if (node && typeof node === "object") {
      const record = node as Record<string, unknown>;
      if (typeof record.type === "string") found.add(record.type);
      Object.values(record).forEach(walk);
    }
  };
  walk(data);
  return found;
}

describe("device type options stay aligned with the backend catalog", () => {
  it("offers only device types that exist in the back-end catalog", () => {
    // 目录里没有的类型，用户选中后只会得到一台 role=None、无额定参数的设备，
    // 外加一条 UNKNOWN_DEVICE_CATALOG——这正是 `水阀` 曾经的处境：
    // 它是**类目名**（DeviceRole.WATER_VALVE 的注释即"水阀（球阀、截止阀、
    // 蝶阀、闸阀、止回阀）"），目录 0 行、Unity 设备库 427 条里没有、
    // 知识库 11 条里也没有。
    const catalog = catalogTypes();
    expect(catalog.size).toBeGreaterThan(0);

    const unknown = DEVICE_TYPE_OPTIONS.filter((type) => !catalog.has(type));
    expect(unknown, `这些可选类型在后端目录里不存在：${unknown}`).toEqual([]);
  });

  it("no longer offers the bare category name 水阀", () => {
    // 回归钉死：`水阀` 是类目名而非设备类型，不得再出现在可选清单里。
    expect(DEVICE_TYPE_OPTIONS).not.toContain("水阀");
    // 取而代之的是目录里真实存在的 5 个水阀类型。
    for (const valve of ["球阀", "截止阀", "蝶阀", "闸阀", "止回阀"]) {
      expect(DEVICE_TYPE_OPTIONS).toContain(valve);
    }
  });

  it("keeps room terminal types a subset of the offered device types", () => {
    // 「房间服务末端」选择器只从这份清单里挑，两者不能脱节。
    for (const type of ROOM_TERMINAL_DEVICE_TYPES) {
      expect(DEVICE_TYPE_OPTIONS).toContain(type);
    }
  });
});
