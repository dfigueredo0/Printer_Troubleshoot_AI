const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Request failed");
  }
  return res.json() as Promise<T>;
}

// ---- types ----------------------------------------------------------------

export interface SessionSummary {
  session_id: string;
  created_at: string;
  os_platform: string;
  symptoms: string[];
  loop_status: string;
  loop_counter: number;
  is_resolved: boolean;
}

export interface ActionEntry {
  entry_id: string;
  specialist: string;
  action: string;
  risk: string;
  status: string;
  confirmation_token: string;
  result?: string;
  timestamp: string;
}

export interface EvidenceItem {
  evidence_id: string;
  specialist: string;
  source: string;
  snippet_id?: string;
  content: string;
  timestamp: string;
}

export interface SnapshotDiff {
  field: string;
  before: unknown;
  after: unknown;
  confirmed_by?: string;
  timestamp: string;
}

export interface SessionState extends SessionSummary {
  user_description: string;
  escalation_reason: string;
  queue_drained: boolean;
  test_print_ok: boolean;
  device_ready: boolean;
  visited_specialists: string[];
  confirmation_tokens: Record<string, string>;
  action_log: ActionEntry[];
  evidence: EvidenceItem[];
  snapshot_diffs: SnapshotDiff[];
  device: Record<string, unknown>;
  network: Record<string, unknown>;
  cups: Record<string, unknown>;
  windows: Record<string, unknown>;
}

export interface AuditTrail {
  session_id: string;
  action_log: ActionEntry[];
  evidence: EvidenceItem[];
  snapshot_diffs: SnapshotDiff[];
  loop_counter: number;
  loop_status: string;
}

export interface DiagnoseResult {
  session_id: string;
  loop_status: string;
  loop_counter: number;
  escalation_reason: string;
  is_resolved: boolean;
}

export interface Snippet {
  snippet_id: string;
  source: string;
  section: string;
  text: string;
  score: number;
}

// ---- API calls ------------------------------------------------------------

export const api = {
  health: () => req<{ status: string; store: string; ts: string }>("/health"),

  listSessions: (limit = 50, offset = 0) =>
    req<{ sessions: SessionSummary[] }>(`/sessions?limit=${limit}&offset=${offset}`),

  createSession: (body: {
    symptoms: string[];
    os_platform: string;
    device_ip: string;
    user_description: string;
  }) => req<{ session_id: string; created_at: string }>("/sessions", { method: "POST", body: JSON.stringify(body) }),

  getSession: (id: string) => req<SessionState>(`/sessions/${id}`),

  deleteSession: (id: string) =>
    fetch(`${BASE}/sessions/${id}`, { method: "DELETE" }).then(() => undefined),

  diagnose: (id: string, force_tier = "auto", max_steps = 10) =>
    req<DiagnoseResult>(`/sessions/${id}/diagnose`, {
      method: "POST",
      body: JSON.stringify({ force_tier, max_steps }),
    }),

  confirm: (id: string, token: string) =>
    req<{ confirmed: boolean; entry_id: string; action: string }>(
      `/sessions/${id}/confirm`,
      { method: "POST", body: JSON.stringify({ token }) }
    ),

  getAudit: (id: string) => req<AuditTrail>(`/sessions/${id}/audit`),

  retrieve: (query: string, top_k = 6) =>
    req<{ query: string; snippets: Snippet[] }>("/retrieve", {
      method: "POST",
      body: JSON.stringify({ query, top_k }),
    }),

  metrics: () => fetch(`${BASE}/metrics`).then((r) => r.text()),
};
