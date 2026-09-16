export interface EquipmentKnowledgeSummary {
  knowledge_id: string;
  device_type: string;
  name: string;
  overview: string;
  catalog_linked: boolean;
  catalog_device_type: string | null;
  applicable_systems: string[];
}

export interface EquipmentKnowledgeDetail extends EquipmentKnowledgeSummary {
  principle: string;
  components: { name: string; function: string }[];
  key_parameters: { key: string; name: string; unit: string | null; description: string; catalog_field: string | null }[];
}

export interface TeachingScenario {
  id: string;
  name: string;
  description: string;
  controls: { scope: string; target_role: string | null; name: string; value: number | "installed_quantity" }[];
  effects: { metric: string; direction: string }[];
}

export interface TeachingComparison {
  project_id: string;
  scenario: TeachingScenario;
  baseline: { metrics: Record<string, number | null>; alarms: string[] };
  scenario_result: { metrics: Record<string, number | null>; alarms: string[] };
  metrics: Record<string, { before: number | null; after: number | null; change: number | null; change_pct: number | null; direction: string | null; unit: string; applicable: boolean }>;
  explanations: { code: string; message: string; related_devices: string[]; related_metrics: string[]; expected_direction: string; observed_direction: string | null; expectation_met: boolean | null }[];
  convergence: { baseline: { converged: boolean; ticks: number; final_room_drift_c: number }; scenario: { converged: boolean; ticks: number; final_room_drift_c: number } };
}

export interface SelectionResult {
  project_id: string;
  target_role: "cold_source" | "heat_source" | "pump" | "terminal";
  status: string;
  requirements: Record<string, number>;
  applied_rules: { capacity_margin: { min: number; max: number }; pump_flow_margin: { min: number; max: number }; pump_head_margin: { min: number; max: number } };
  candidates: { rank: number; device_type: string; model_name: string; role: string; quantity: number; rated: Record<string, number | null>; provided: Record<string, number | null>; margins: Record<string, number | null>; eligible: boolean; reasons: { code: string; message: string }[] }[];
}
