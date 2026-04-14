"use client";

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";
import { api, SessionSummary } from "@/lib/api";

type Tab = "sessions" | "config" | "metrics";

// ---- Config viewer --------------------------------------------------------

const CONFIG_KEYS = [
  { label: "API URL", key: "NEXT_PUBLIC_API_URL", value: process.env.NEXT_PUBLIC_API_URL ?? "(unset)" },
];

function ConfigViewer() {
  const [health, setHealth] = useState<Record<string, unknown> | null>(null);
  useEffect(() => {
    api.health().then(setHealth).catch(() => {});
  }, []);

  return (
    <div className="space-y-4">
      <div className="bg-gray-50 rounded-xl border border-gray-200 p-4">
        <h3 className="text-sm font-semibold mb-3">Service Health</h3>
        {health ? (
          <pre className="text-xs overflow-auto">{JSON.stringify(health, null, 2)}</pre>
        ) : (
          <p className="text-sm text-gray-400">Connecting…</p>
        )}
      </div>
      <div className="bg-gray-50 rounded-xl border border-gray-200 p-4">
        <h3 className="text-sm font-semibold mb-3">Frontend Config</h3>
        <table className="w-full text-sm">
          <tbody>
            {CONFIG_KEYS.map(({ label, value }) => (
              <tr key={label} className="border-b border-gray-100 last:border-0">
                <td className="py-2 font-medium text-gray-500 pr-4">{label}</td>
                <td className="py-2 font-mono text-gray-700">{value}</td>
              </tr>
            ))}
            {health && Object.entries(health).map(([k, v]) => (
              <tr key={k} className="border-b border-gray-100 last:border-0">
                <td className="py-2 font-medium text-gray-500 pr-4">{k}</td>
                <td className="py-2 font-mono text-gray-700">{String(v)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---- Metrics viewer -------------------------------------------------------

function MetricsViewer() {
  const [raw, setRaw] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function fetch_() {
    setLoading(true);
    try {
      const text = await api.metrics();
      setRaw(text);
    } catch {
      setRaw("Could not fetch metrics.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <button
          onClick={fetch_}
          disabled={loading}
          className="text-sm bg-brand hover:bg-brand-dark disabled:opacity-50 text-white px-4 py-2 rounded-lg transition-colors"
        >
          {loading ? "Loading…" : "Fetch Prometheus Metrics"}
        </button>
        {raw && (
          <span className="text-xs text-gray-400">
            {raw.split("\n").length} lines
          </span>
        )}
      </div>
      {raw && (
        <pre className="bg-gray-50 border border-gray-200 rounded-xl p-4 text-xs overflow-auto max-h-96">
          {raw}
        </pre>
      )}
    </div>
  );
}

// ---- Session browser ------------------------------------------------------

const STATUS_COLORS: Record<string, string> = {
  running: "bg-blue-100 text-blue-700",
  success: "bg-green-100 text-green-700",
  escalated: "bg-orange-100 text-orange-700",
  max_steps: "bg-yellow-100 text-yellow-700",
};

function SessionBrowser() {
  const router = useRouter();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("");
  const [deleting, setDeleting] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.listSessions(200, 0);
      setSessions(data.sessions);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const filtered = sessions.filter(
    (s) =>
      !filter ||
      s.session_id.includes(filter) ||
      s.os_platform.includes(filter) ||
      s.loop_status.includes(filter) ||
      s.symptoms.some((sym) => sym.toLowerCase().includes(filter.toLowerCase()))
  );

  async function handleDelete(id: string) {
    if (!confirm("Delete session " + id + "?")) return;
    setDeleting(id);
    try {
      await api.deleteSession(id);
      setSessions((prev) => prev.filter((s) => s.session_id !== id));
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <input
          type="text"
          placeholder="Filter by ID, platform, status, or symptom…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="border rounded-lg px-3 py-2 text-sm flex-1 focus:outline-none focus:ring-2 focus:ring-brand/40"
        />
        <button
          onClick={load}
          className="text-sm border border-gray-200 hover:bg-gray-50 px-4 py-2 rounded-lg transition-colors"
        >
          Refresh
        </button>
        <span className="text-sm text-gray-400">{filtered.length} / {sessions.length}</span>
      </div>

      {loading ? (
        <p className="text-sm text-gray-400">Loading…</p>
      ) : (
        <div className="overflow-hidden rounded-xl border border-gray-200">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-gray-500">ID</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Platform</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Symptoms</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Status</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Steps</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Created</th>
                <th className="px-4 py-3"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {filtered.map((s) => (
                <tr key={s.session_id} className="hover:bg-gray-50 transition-colors">
                  <td
                    className="px-4 py-3 font-mono text-xs text-brand cursor-pointer hover:underline"
                    onClick={() => router.push(`/session/${s.session_id}`)}
                  >
                    {s.session_id.slice(0, 8)}…
                  </td>
                  <td className="px-4 py-3 capitalize">{s.os_platform}</td>
                  <td className="px-4 py-3 text-gray-600 max-w-xs truncate">
                    {s.symptoms.slice(0, 2).join(", ")}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_COLORS[s.loop_status] ?? "bg-gray-100 text-gray-600"}`}>
                      {s.is_resolved ? "resolved" : s.loop_status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-gray-600">{s.loop_counter}</td>
                  <td className="px-4 py-3 text-gray-400 text-xs">
                    {new Date(s.created_at).toLocaleString()}
                  </td>
                  <td className="px-4 py-3">
                    <button
                      disabled={deleting === s.session_id}
                      onClick={() => handleDelete(s.session_id)}
                      className="text-xs text-red-400 hover:text-red-600 disabled:opacity-50 transition-colors"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---- Page -----------------------------------------------------------------

export default function AdminPage() {
  const [tab, setTab] = useState<Tab>("sessions");

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">Admin Dashboard</h1>
      <div className="flex border-b border-gray-200 mb-6">
        {(["sessions", "config", "metrics"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-5 py-3 text-sm font-medium capitalize transition-colors ${
              tab === t
                ? "border-b-2 border-brand text-brand"
                : "text-gray-500 hover:text-gray-700"
            }`}
          >
            {t === "sessions" ? "Session Browser" : t === "config" ? "Runtime Config" : "Metrics"}
          </button>
        ))}
      </div>
      {tab === "sessions" && <SessionBrowser />}
      {tab === "config" && <ConfigViewer />}
      {tab === "metrics" && <MetricsViewer />}
    </div>
  );
}
