import type { Issue, NetworkPathCalculation } from "@/types";

export interface AirNetworkResult {
  total_airflow_m3_h: number;
  ducts: Array<{
    duct_id: string;
    airflow_m3_h: number;
    input_airflow_m3_h: number | null;
    flow_source: string;
    network_side: "supply" | "return" | "unknown";
    size: Record<string, unknown>;
    velocity_m_s: number;
    pressure_drop_pa: number;
  }>;
  paths: NetworkPathCalculation[];
  required_fan_pressure_pa: number;
  most_unfavorable_path_id: string | null;
  fan_check: {
    required_airflow_m3_h: number;
    installed_airflow_m3_h: number;
    required_pressure_pa: number;
    available_pressure_pa: number;
    airflow_margin: number | null;
    pressure_margin: number | null;
    status: "ok" | "warning" | "error";
  } | null;
  balancing: {
    path_count: number;
    min_pressure_drop_pa: number;
    max_pressure_drop_pa: number;
    imbalance_ratio: number;
    path_balancing_pressure_pa: Record<string, number>;
  };
  solver: {
    method: "directed_tree_aggregation" | "legacy_bounded_fallback";
    succeeded: boolean;
    degraded: boolean;
    steps: number;
    max_flow_residual_m3_h: number;
    flow_demand_sources: Record<string, string>;
    fallback_used: boolean;
  };
  node_balances: Array<{
    node_id: string;
    network_side: "supply" | "return";
    root_id: string;
    inflow_m3_h: number;
    outflow_m3_h: number;
    residual_m3_h: number;
  }>;
  floor_airflow_summary: Record<string, Record<string, number>>;
  issues: Issue[];
}

export interface EvaluationReport {
  summary: Record<string, unknown>;
  equipment: Record<string, unknown>;
  hydraulic: Record<string, unknown> | null;
  air: Record<string, unknown> | null;
  issues: Issue[];
  recommendations: string[];
}
