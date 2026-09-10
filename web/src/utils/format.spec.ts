import { describe, expect, it } from "vitest";
import { designParamLabel, finite, firstNumeric, paramLabel, runtimeFinite, valueWithUnit } from "./format";

describe("runtime number formatting", () => {
  it("does not expose invalid numeric values", () => {
    expect(finite(Number.NaN)).toBe("--");
    expect(finite(null)).toBe("--");
  });

  it("keeps units stable", () => {
    expect(valueWithUnit(12.345, "kW", 1)).toBe("12.3 kW");
  });

  it("projects the first available runtime variable", () => {
    expect(firstNumeric({ temperature_c: 24.5 }, ["room_temp_c", "temperature_c"])).toBe(24.5);
  });

  it("keeps small runtime changes visible by field meaning", () => {
    expect(runtimeFinite("power_kw", 0.0049)).toBe("0.005");
    expect(runtimeFinite("load_ratio", 0.1236)).toBe("0.124");
    expect(runtimeFinite("enabled", 1)).toBe("1");
    expect(runtimeFinite("power_kw", Number.POSITIVE_INFINITY)).toBe("--");
  });
});

describe("Chinese param labels", () => {
  it("translates known runtime field names", () => {
    expect(paramLabel("supply_temp_c")).toBe("供水温度 (°C)");
    expect(paramLabel("cop")).toBe("COP");
    expect(paramLabel("power_kw")).toBe("输入功率 (kW)");
  });

  it("resolves role-specific runtime fields", () => {
    expect(paramLabel("flow_m3_h", "cold_source")).toBe("冷冻水流量 (m³/h)");
    expect(paramLabel("flow_m3_h", "heat_source")).toBe("热水流量 (m³/h)");
    expect(paramLabel("flow_m3_h", "cooling_tower")).toBe("冷却水流量 (m³/h)");
    expect(paramLabel("flow_m3_h", "pump")).toBe("流量 (m³/h)");
    expect(paramLabel("power_kw", "pump")).toBe("轴功率 (kW)");
    expect(paramLabel("flow_m3_h", "terminal")).toBe("水流量 (m³/h)");
    expect(paramLabel("power_kw", "terminal")).toBe("风机功率 (kW)");
  });

  it("falls back to the raw key for unknown fields", () => {
    expect(paramLabel("custom_unmapped") === "custom_unmapped").toBe(true);
    expect(paramLabel("flow_m3_h", "unknown_role")).toBe("水流量 (m³/h)");
  });

  it("translates design attribute field names", () => {
    expect(designParamLabel("capacity_kw")).toBe("额定容量 (kW)");
    expect(designParamLabel("head_m")).toBe("额定扬程 (m)");
    expect(designParamLabel("quantity")).toBe("台数");
    expect(designParamLabel("custom_unmapped") === "custom_unmapped").toBe(true);
  });
});
