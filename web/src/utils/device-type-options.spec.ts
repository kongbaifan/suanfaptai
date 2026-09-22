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

  it("offers the accessories that have no other representation", () => {
    // 这 4 类此前选不到，但它们在本平台里没有别的表达方式：
    //   分水器/集水器 —— 多支路分配（按「管路 + pipe_type」接法可用）
    //   防火阀       —— 风管上的防火分隔
    //   测量件       —— 温度计/压力计/流量计测点
    for (const type of ["分水器", "集水器", "防火阀", "测量件"]) {
      expect(DEVICE_TYPE_OPTIONS).toContain(type);
    }
  });

  it("deliberately keeps pipe fittings out of the device list", () => {
    // 管件在平台里由「管路」表达（PipeInput + PIPE_TYPES：主供水/主回水/
    // 支路供水/支路回水/末端支路/立管/旁通），不是设备。补进清单等于让同一段
    // 管子有两套建法，必然漂移（本仓库已在「两张漂移的别名表」上栽过一次）。
    const fittings = [
      "圆直管",
      "定位圆管",
      "定位方管",
      "定位铜管",
      "圆三通",
      "圆四通",
      "方三通",
      "方四通",
      "铜异径管",
      "分歧管",
    ];
    for (const type of fittings) {
      expect(DEVICE_TYPE_OPTIONS).not.toContain(type);
    }

    // 与「列了根本不存在的类型」（`水阀`）区分开：这些类型在目录里**确实存在**，
    // 本次是**有意不加**。若哪天目录里它们消失了，这条断言会失败并提醒复核理由。
    const catalog = catalogTypes();
    for (const type of fittings) {
      expect(catalog.has(type), `${type} 已不在后端目录里，需要复核「有意不加」的理由`).toBe(true);
    }
  });
});
