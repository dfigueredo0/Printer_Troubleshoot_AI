"use client";

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";
import { api, SessionSummary } from "@/lib/api";

const PLATFORMS = ["unknown", "windows", "linux", "macos"] as const;

const STATUS_COLORS: Record<string, string> = {
  running: "bg-blue-100 text-blue-700",
  success: "bg-green-100 text-green-700",
  escalated: "bg-orange-100 text-orange-700",
  max_steps: "bg-yellow-100 text-yellow-700",
  timeout: "bg-red-100 text-red-700",
};

export default function HomePage() {
  const router = useRouter();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [showForm, setShowForm] = useState(false);

  const [symptoms, setSymptoms] = useState("");
  const [platform, setPlatform] = useState<string>("unknown");
  const [deviceIp, setDeviceIp] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");

  const fetchSessions = useCallback(async () => {
    try {
      const data = await api.listSessions();
      setSessions(data.sessions);
    } catch {
      // service may be unreachable on first load
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setCreating(true);
    setError("");
    try {
      const { session_id } = await api.createSession({
        symptoms: symptoms.split("\n").map((s) => s.trim()).filter(Boolean),
        os_platform: platform,
        device_ip: deviceIp || "unknown",
        user_description: description,
      });
      router.push(`/session/${session_id}`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to create session");
      setCreating(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Troubleshooting Sessions</h1>
          <p className="text-sm text-gray-500 mt-1">
            Start a new session or continue an existing one.
          </p>
        </div>
        <button
          onClick={() => setShowForm((v) => !v)}
          className="bg-brand hover:bg-brand-dark text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors"
        >
          {showForm ? "Cancel" : "New Session"}
        </button>
      </div>

      {showForm && (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
          <h2 className="text-lg font-semibold mb-4">New Troubleshooting Session</h2>
          <form onSubmit={handleCreate} className="space-y-4">
            <div>
              <label className="block text-sm font-medium mb-1">
                Symptoms <span className="text-gray-400 font-normal">(one per line)</span>
              </label>
              <textarea
                className="w-full border rounded-lg px-3 py-2 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-brand/40"
                rows={3}
                placeholder="e.g. Printer not responding&#10;Error light flashing&#10;Labels not feeding"
                value={symptoms}
                onChange={(e) => setSymptoms(e.target.value)}
                required
              />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium mb-1">OS Platform</label>
                <select
                  className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand/40"
                  value={platform}
                  onChange={(e) => setPlatform(e.target.value)}
                >
                  {PLATFORMS.map((p) => (
                    <option key={p} value={p}>{p.charAt(0).toUpperCase() + p.slice(1)}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium mb-1">Device IP</label>
                <input
                  type="text"
                  className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand/40"
                  placeholder="192.168.1.100"
                  value={deviceIp}
                  onChange={(e) => setDeviceIp(e.target.value)}
                />
              </div>
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Description</label>
              <textarea
                className="w-full border rounded-lg px-3 py-2 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-brand/40"
                rows={2}
                placeholder="Any additional context about the issue..."
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            {error && (
              <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
                {error}
              </p>
            )}
            <div className="flex justify-end">
              <button
                type="submit"
                disabled={creating}
                className="bg-brand hover:bg-brand-dark disabled:opacity-50 text-white px-6 py-2 rounded-lg text-sm font-medium transition-colors"
              >
                {creating ? "Creating..." : "Start Session"}
              </button>
            </div>
          </form>
        </div>
      )}

      {loading ? (
        <div className="text-sm text-gray-400">Loading sessions...</div>
      ) : sessions.length === 0 ? (
        <div className="bg-white rounded-xl border border-gray-200 p-12 text-center text-gray-400">
          <p className="text-lg font-medium">No sessions yet</p>
          <p className="text-sm mt-1">Click "New Session" to start troubleshooting.</p>
        </div>
      ) : (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Session ID</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Platform</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Symptoms</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Status</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Steps</th>
                <th className="px-4 py-3 text-left font-medium text-gray-500">Created</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {sessions.map((s) => (
                <tr
                  key={s.session_id}
                  className="hover:bg-gray-50 cursor-pointer transition-colors"
                  onClick={() => router.push(`/session/${s.session_id}`)}
                >
                  <td className="px-4 py-3 font-mono text-xs text-gray-500">
                    {s.session_id.slice(0, 8)}…
                  </td>
                  <td className="px-4 py-3 capitalize">{s.os_platform}</td>
                  <td className="px-4 py-3 text-gray-600 max-w-xs truncate">
                    {s.symptoms.slice(0, 2).join(", ")}{s.symptoms.length > 2 ? "…" : ""}
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
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
