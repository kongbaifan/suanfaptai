// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DiagnosticsView from "./DiagnosticsView.vue";
import { api } from "@/api/client";
import type { Diagnostics } from "@/types";

vi.mock("@/api/client", () => ({
  api: vi.fn(),
  errorMessage: (reason: unknown) => String(reason),
}));

const apiMock = vi.mocked(api);

function report(overrides: Partial<Diagnostics> = {}): Diagnostics {
  return {
    status: "ok",
    service: {
      product: "HVACAlgorithmService",
      app_version: "0.1.0",
      project_schema_version: "1.0",
      python_version: "3.13.1",
      platform: "Windows 11",
      frozen: false,
      uptime_seconds: 42,
    },
    checks: [
      { name: "release_manifest", ok: true, required: true, detail: "version fields match runtime constants" },
      { name: "resource:app/data/rules.json", ok: true, required: true, detail: "present" },
      { name: "data_directory", ok: true, required: true, detail: "writable" },
    ],
    data: {
      catalog_items: 53,
      catalog_types: 21,
      catalog_issues: 0,
      knowledge_entries: 11,
      stored_projects: 12,
    },
    realtime: {
      active_sessions: 0,
      running_simulations: 0,
      websocket_subscribers: 0,
      projects: [],
    },
    ...overrides,
  };
}

describe("service diagnostics view", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    apiMock.mockReset();
  });

  it("renders environment, data scale and passing checks", async () => {
    apiMock.mockResolvedValue(report());
    const wrapper = mount(DiagnosticsView);
    await flushPromises();

    expect(apiMock).toHaveBeenCalledWith("/api/v1/diagnostics");
    expect(wrapper.text()).toContain("服务自诊断");
    expect(wrapper.text()).toContain("HVACAlgorithmService");
    expect(wrapper.text()).toContain("3.13.1");
    expect(wrapper.text()).toContain("53");
    expect(wrapper.text()).toContain("当前没有活动的实时仿真会话");
    // 资源检查项应去掉 "resource:" 前缀后展示
    expect(wrapper.text()).toContain("app/data/rules.json");
    expect(wrapper.text()).not.toContain("resource:app/data/rules.json");
    wrapper.unmount();
  });

  it("surfaces failed checks in a callout and marks required ones", async () => {
    apiMock.mockResolvedValue(
      report({
        status: "error",
        checks: [
          { name: "release_manifest", ok: false, required: true, detail: "JSONDecodeError: broken" },
          { name: "content:app/data/equipment_catalog.json", ok: false, required: false, detail: "entries=52; expected=53" },
        ],
      }),
    );
    const wrapper = mount(DiagnosticsView);
    await flushPromises();

    expect(wrapper.text()).toContain("2 项检查未通过");
    expect(wrapper.text()).toContain("JSONDecodeError: broken");
    expect(wrapper.text()).toContain("（必需）");
    expect(wrapper.text()).toContain("（参考）");
    expect(wrapper.text()).toContain("异常");
    wrapper.unmount();
  });

  it("lists active realtime sessions with tick and subscriber counts", async () => {
    apiMock.mockResolvedValue(
      report({
        realtime: {
          active_sessions: 2,
          running_simulations: 1,
          websocket_subscribers: 3,
          projects: [
            { project_id: "pjt-a", tick: 120, running: true, subscribers: 2, updated_at: "2026-08-24T10:00:00Z" },
            { project_id: "pjt-b", tick: 0, running: false, subscribers: 1, updated_at: null },
          ],
        },
      }),
    );
    const wrapper = mount(DiagnosticsView);
    await flushPromises();

    expect(wrapper.text()).toContain("pjt-a");
    expect(wrapper.text()).toContain("tick 120");
    expect(wrapper.text()).toContain("运行中");
    expect(wrapper.text()).toContain("已停止");
    expect(wrapper.text()).toContain("3 个订阅");
    wrapper.unmount();
  });

  it("shows a read failure instead of a bogus zero count", async () => {
    apiMock.mockResolvedValue(
      report({ data: { catalog_items: -1, catalog_types: -1, catalog_issues: 0, knowledge_entries: 11, stored_projects: 12 } }),
    );
    const wrapper = mount(DiagnosticsView);
    await flushPromises();

    expect(wrapper.text()).toContain("读取失败");
    wrapper.unmount();
  });

  it("reports the API error when the endpoint fails", async () => {
    apiMock.mockRejectedValue(new Error("boom"));
    const wrapper = mount(DiagnosticsView);
    await flushPromises();

    expect(wrapper.text()).toContain("诊断接口调用失败");
    wrapper.unmount();
  });
});
