// @vitest-environment jsdom

import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CatalogView from "./CatalogView.vue";
import { api } from "@/api/client";

vi.mock("@/api/client", () => ({
  api: vi.fn(),
  errorMessage: (reason: unknown) => String(reason),
}));

const apiMock = vi.mocked(api);
const catalog = [
  { "设备类型": "冷源机组", "型号": "30XW-P0312P", "兼容旧型号": ["LG340MCF"], "设备角色": "cold_source", "额定容量": 303.5, "性能系数": 5.65, "输入功率": 53.8, "适用系统": ["chilled_water", "cooling_water"], "品牌": "Carrier", "厂家资料": "https://www.carrier.com/30xw.pdf" },
  { "设备类型": "冷源机组", "型号": "30XW-P0352P", "设备角色": "cold_source", "额定容量": 366.3, "性能系数": 5.65, "输入功率": 64.8, "适用系统": ["chilled_water", "cooling_water"], "品牌": "Carrier" },
  { "设备类型": "冷源机组", "型号": "*", "设备角色": "cold_source", "额定容量": 340, "适用系统": ["chilled_water", "cooling_water"] },
  { "设备类型": "风机盘管", "型号": "FP-204WA", "兼容旧型号": ["MCW1000V", "MCW1200V"], "设备角色": "terminal", "额定容量": 11.0, "额定风量": 2040, "水流量": 1.892, "风机功率": 0.157, "噪声": 55, "适用系统": ["chilled_water", "hot_water", "air"], "系列": "FP-WA" },
  { "设备类型": "空气源热泵", "型号": "CGK-100V3L-B", "兼容旧型号": ["ASHP-240"], "设备角色": "heat_source", "额定容量": 34.5, "额定制热量": 34.5, "额定流量": 5.94, "输入功率": 7.841, "制热输入功率": 7.841, "性能系数": 4.4, "额定制冷量": 20.8, "制冷输入功率": 8, "EER": 2.6, "制冷蒸发器进水温度": 12, "制冷蒸发器出水温度": 7, "制冷室外干球温度": 35, "制热室外干球温度": 7, "制热冷凝器进水温度": 30, "制热冷凝器出水温度": 35, "适用系统": ["chilled_water", "hot_water"], "品牌": "SPRSUN" },
  { "设备类型": "蝶阀", "型号": "*", "设备角色": "water_accessory", "局部阻力系数": 1.8, "适用系统": ["chilled_water"] },
];

describe("equipment catalog cards", () => {
  beforeEach(() => {
    apiMock.mockResolvedValue(catalog);
  });

  it("renders equipment roles without the network and accessory group", async () => {
    const wrapper = mount(CatalogView);
    await flushPromises();

    expect(wrapper.findAll(".catalog-equipment-card")).toHaveLength(3);
    expect(wrapper.text()).toContain("冷热源与输配系统");
    expect(wrapper.text()).toContain("空气处理与末端");
    expect(wrapper.text()).not.toContain("管网与附件");
    expect(wrapper.text()).not.toContain("蝶阀");
    expect(wrapper.text()).toContain("30XW-P0312P");
    expect(wrapper.text()).not.toContain("LG340MCF");
    expect(wrapper.text()).not.toContain("通用参数");
    expect(wrapper.text()).toContain("制冷工况");
    expect(wrapper.text()).toContain("制热工况");
    wrapper.unmount();
  });

  it("switches models and displays backend rated parameters with units", async () => {
    const wrapper = mount(CatalogView);
    await flushPromises();

    const chiller = wrapper.findAll(".catalog-equipment-card").find((card) => card.text().includes("冷源机组"));
    expect(chiller).toBeTruthy();
    expect(chiller!.text()).toContain("303.5 kW");
    expect(chiller!.text()).toContain("5.65");

    await chiller!.find("select").setValue("30XW-P0352P");
    expect(chiller!.text()).toContain("366.3 kW");
    expect(chiller!.text()).toContain("64.8 kW");
    wrapper.unmount();
  });

  it("renders complete air-source heat pump operating modes from backend values", async () => {
    const wrapper = mount(CatalogView);
    await flushPromises();

    const heatPump = wrapper.findAll(".catalog-equipment-card").find((card) => card.text().includes("空气源热泵"));
    expect(heatPump).toBeTruthy();
    expect(heatPump!.findAll(".catalog-mode")).toHaveLength(2);
    expect(heatPump!.text()).toContain("34.5 kW");
    expect(heatPump!.text()).toContain("7.841 kW");
    const [coolingMode, heatingMode] = heatPump!.findAll(".catalog-mode");
    expect(coolingMode.text()).toContain("20.8 kW");
    expect(coolingMode.text()).toContain("8 kW");
    expect(coolingMode.text()).toContain("2.6");
    expect(coolingMode.text()).toContain("蒸发器进 / 出水 (℃)");
    expect(coolingMode.text()).toContain("12 / 7 ℃");
    expect(coolingMode.text()).toContain("冷凝器空气侧室外干球温度 (℃)");
    expect(coolingMode.text()).toContain("35 ℃");
    expect(heatingMode.text()).toContain("蒸发器空气侧室外干球温度 (℃)");
    expect(heatingMode.text()).toContain("7 ℃");
    expect(heatingMode.text()).toContain("30 / 35 ℃");
    expect(heatPump!.text()).not.toContain("--");
    wrapper.unmount();
  });

  it("shows only manufacturer-published water-source temperatures", async () => {
    apiMock.mockResolvedValue([
      { "设备类型": "水源热泵", "型号": "CGS-42(HC)", "设备角色": "heat_source", "额定容量": 46, "输入功率": 8.78, "性能系数": 5.24, "额定制冷量": 39.1, "制冷输入功率": 8.58, "EER": 4.56, "制热蒸发器进水温度": 15, "制热蒸发器出水温度": 10, "制热冷凝器进水温度": 15, "制热冷凝器出水温度": 55, "适用系统": ["chilled_water", "hot_water"], "品牌": "SPRSUN" },
    ]);
    const wrapper = mount(CatalogView);
    await flushPromises();

    const [coolingMode, heatingMode] = wrapper.findAll(".catalog-mode");
    expect(coolingMode.text()).toContain("蒸发器进 / 出水 (℃)--");
    expect(coolingMode.text()).toContain("冷凝器进 / 出水 (℃)--");
    expect(heatingMode.text()).toContain("15 / 10 ℃");
    expect(heatingMode.text()).toContain("15 / 55 ℃");
    expect(wrapper.text()).not.toContain("空气侧");
    wrapper.unmount();
  });

  it("filters by group and searches backend model values", async () => {
    const wrapper = mount(CatalogView);
    await flushPromises();

    await wrapper.find("select[aria-label='设备分组']").setValue("air");
    expect(wrapper.findAll(".catalog-equipment-card")).toHaveLength(1);
    expect(wrapper.text()).toContain("风机盘管");

    await wrapper.find("input[type='search']").setValue("MCW1000V");
    expect(wrapper.findAll(".catalog-equipment-card")).toHaveLength(1);
    expect(wrapper.text()).not.toContain("MCW1000V");
    expect(wrapper.find("select[aria-label='风机盘管设备型号']").text()).toBe("FP-204WA");
    wrapper.unmount();
  });

  it("renders every terminal parameter the backend returns", async () => {
    // 后端把 water_flow_m3_h 译为「水流量」而非「额定水量」；标签名对不上会让这一行静默消失。
    const wrapper = mount(CatalogView);
    await flushPromises();

    const fcu = wrapper.findAll(".catalog-equipment-card").find((card) => card.text().includes("风机盘管"));
    expect(fcu).toBeTruthy();
    expect(fcu!.text()).toContain("水流量");
    expect(fcu!.text()).toContain("1.892 m³/h");
    expect(fcu!.text()).toContain("11 kW");
    expect(fcu!.text()).toContain("2,040 m³/h");
    expect(fcu!.text()).toContain("0.157 kW");
    expect(fcu!.text()).toContain("55 dB(A)");
    // 样本无制热表，风机盘管不应凭空出现制热行
    expect(fcu!.text()).not.toContain("额定制热量");
    // 额定工况已从目录移除，卡片不应再出现该行
    expect(fcu!.text()).not.toContain("额定工况");
    wrapper.unmount();
  });

  it("drops the card footer without leaking its fields into the parameter list", async () => {
    // 页脚移除后，品牌/厂家资料/适用系统必须仍在 metadata 排除集合里，
    // 否则它们会从页脚跑到参数行里，等于没删。
    const wrapper = mount(CatalogView);
    await flushPromises();

    expect(wrapper.find(".catalog-card-footer").exists()).toBe(false);
    const chiller = wrapper.findAll(".catalog-equipment-card").find((card) => card.text().includes("冷源机组"));
    expect(chiller).toBeTruthy();
    expect(chiller!.text()).not.toContain("Carrier");
    expect(chiller!.text()).not.toContain("厂家资料");
    expect(chiller!.text()).not.toContain("chilled_water");
    expect(chiller!.findAll("a")).toHaveLength(0);
    // 参数本身不受影响
    expect(chiller!.text()).toContain("303.5 kW");
    wrapper.unmount();
  });

  it("keeps the view read-only except the unity sync action", async () => {
    const wrapper = mount(CatalogView);
    await flushPromises();

    // 刷新按钮只 GET；同步按钮是唯一的 POST 动作，不发请求即保持只读。
    expect(wrapper.findAll("button")).toHaveLength(2);
    expect(apiMock.mock.calls.every(([, init]) => init === undefined)).toBe(true);
    wrapper.unmount();
  });

  it("hides unity placement items and shows them via toggle", async () => {
    apiMock.mockResolvedValue([
      ...catalog,
      { "设备类型": "冷源机组", "型号": "LG340MCF", "设备角色": "cold_source", "仅摆放": true, "适用系统": ["chilled_water", "cooling_water", "hot_water"] },
    ]);
    const wrapper = mount(CatalogView);
    await flushPromises();

    expect(wrapper.find(".catalog-model-names").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("LG340MCF");
    expect(wrapper.text()).toContain("显示 Unity 摆放件（1）");

    await wrapper.find('input[type="checkbox"]').setValue(true);
    expect(wrapper.find(".catalog-model-names").text()).toContain("LG340MCF");
    // 工程参数卡片不吸收摆放条目：冷源机组仍只有一张"设备型号"卡片（LG 在摆放区用"摆放型号"）。
    const chiller = wrapper.findAll(".catalog-equipment-card").filter((card) => card.text().includes("设备型号") && card.text().includes("冷源机组"));
    expect(chiller).toHaveLength(1);
    expect(chiller[0]!.text()).not.toContain("LG340MCF");
    wrapper.unmount();
  });

  it("reports unity sync result and reloads the catalog", async () => {
    const wrapper = mount(CatalogView);
    await flushPromises();
    apiMock.mockClear();
    apiMock.mockImplementation(((path: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return Promise.resolve({ import: { attached: 23, created: 404, total: 427 }, export: { count: 427, changed: true } }) as never;
      }
      return Promise.resolve(catalog) as never;
    }) as typeof api);

    await wrapper.findAll("button")[0]!.trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("已同步 Unity 设备库：主库吸收 427 条（新增摆放条目 404 条），导出 427 条。");
    expect(apiMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true);
    wrapper.unmount();
  });
});
