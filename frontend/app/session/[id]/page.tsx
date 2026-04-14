"use client";

import { useState, useEffect, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import { api, SessionState, AuditTrail, ActionEntry } from "@/lib/api";

// ---- sub-components -------------------------------------------------------

function Badge({ label, color }: { label: string; color: string }) {
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${color}`}>
      {label}
    </span>
  );
}

const RISK_COLORS: Record<string, string> = {
  safe: "bg-green-100 text-green-700",
  low: "bg-blue-100 text-blue-700",
  medium: "bg-yellow-100 text-yellow-700",
  destructive: "bg-red-100 text-red-700",
  config_change: "bg-orange-100 text-orange-700",
  firmware: "bg-purple-100 text-purple-700",
  reboot: "bg-pink-100 text-pink-700",
  service_restart: "bg-orange-100 text-orange-700",
};

const STATUS_COLORS: Record<string, string> = {
  running: "bg-blue-100 text-blue-700",
  success: "bg-green-100 text-green-700",
  escalated: "bg-orange-100 text-orange-700",
  max_steps: "bg-yellow-100 text-yellow-700",
};

function EvidencePanel({ items }: { items: SessionState["evidence"] }) {
  if (items.length === 0)
    return <p className="text-sm text-gray-400">No evidence collected yet.</p>;
  return (
    <ul className="space-y-2">
      {items.map((e) => (
        <li key={e.evidence_id} className="border border-gray-100 rounded-lg p-3">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-xs font-medium text-blue-600">{e.specialist}</span>
            <span className="text-xs text-gray-400">·</span>
            <span className="text-xs text-gray-400">{e.source}</span>
            {e.snippet_id && (
              <span className="text-xs text-gray-300">[{e.snippet_id}]</span>
            )}
          </div>
          <p className="text-sm text-gray-700 whitespace-pre-wrap">{e.content}</p>
          <p className="text-xs text-gray-300 mt-1">{new Date(e.timestamp).toLocaleString()}</p>
        </li>
      ))}
    </ul>
  );
}

function AuditViewer({ audit }: { audit: AuditTrail | null }) {
  if (!audit) return <p className="text-sm text-gray-400">No audit data yet.</p>;
  return (
    <div className="space-y-4">
      <div>
        <h4 className="text-sm font-semibold mb-2">Action Log</h4>
        {audit.action_log.length === 0 ? (
          <p className="text-sm text-gray-400">No actions recorded.</p>
        ) : (
          <ul className="space-y-2">
            {audit.action_log.map((a) => (
              <li key={a.entry_id} className="border border-gray-100 rounded-lg p-3">
                <div className="flex items-center gap-2 flex-wrap mb-1">
                  <span className="text-xs font-medium">{a.specialist}</span>
                  <Badge label={a.risk} color={RISK_COLORS[a.risk] ?? "bg-gray-100 text-gray-600"} />
                  <Badge
                    label={a.status}
                    color={a.status === "confirmed" || a.status === "executed"
                      ? "bg-green-100 text-green-700"
                      : a.status === "failed"
                      ? "bg-red-100 text-red-700"
                      : "bg-gray-100 text-gray-600"}
                  />
                </div>
                <p className="text-sm">{a.action}</p>
                {a.result && <p className="text-xs text-gray-400 mt-1">{a.result}</p>}
                {a.confirmation_token && (
                  <p className="text-xs font-mono text-orange-500 mt-1">
                    Token: {a.confirmation_token}
                  </p>
                )}
                <p className="text-xs text-gray-300 mt-1">{new Date(a.timestamp).toLocaleString()}</p>
              </li>
            ))}
          </ul>
        )}
      </div>
      {audit.snapshot_diffs.length > 0 && (
        <div>
          <h4 className="text-sm font-semibold mb-2">State Changes</h4>
          <ul className="space-y-1">
            {audit.snapshot_diffs.map((d, i) => (
              <li key={i} className="text-sm border-l-2 border-blue-200 pl-3 py-1">
                <span className="font-medium">{d.field}</span>
                <span className="text-gray-400 mx-1">→</span>
                <span className="text-gray-600">{JSON.stringify(d.after)}</span>
                {d.confirmed_by && (
                  <span className="text-xs text-gray-400 ml-2">(by {d.confirmed_by})</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function ConfirmationModal({
  entries,
  onConfirm,
  onClose,
}: {
  entries: ActionEntry[];
  onConfirm: (token: string) => Promise<void>;
  onClose: () => void;
}) {
  const [confirming, setConfirming] = useState<string | null>(null);

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl shadow-xl max-w-lg w-full p-6">
        <h3 className="text-lg font-semibold mb-1">Actions Pending Approval</h3>
        <p className="text-sm text-gray-500 mb-4">
          The following actions require operator confirmation before execution.
        </p>
        <ul className="space-y-3 mb-6">
          {entries.map((e) => (
            <li key={e.entry_id} className="border rounded-lg p-3">
              <div className="flex items-center gap-2 mb-1">
                <span className="font-medium text-sm">{e.specialist}</span>
                <Badge label={e.risk} color={RISK_COLORS[e.risk] ?? "bg-gray-100 text-gray-600"} />
              </div>
              <p className="text-sm text-gray-700">{e.action}</p>
              <div className="flex items-center justify-between mt-2">
                <span className="text-xs font-mono text-gray-400">{e.confirmation_token}</span>
                <button
                  disabled={!!confirming}
                  onClick={async () => {
                    setConfirming(e.entry_id);
                    await onConfirm(e.confirmation_token);
                    setConfirming(null);
                  }}
                  className="text-xs bg-green-600 hover:bg-green-700 disabled:opacity-50 text-white px-3 py-1 rounded-md transition-colors"
                >
                  {confirming === e.entry_id ? "Confirming…" : "Approve"}
                </button>
              </div>
            </li>
          ))}
        </ul>
        <div className="flex justify-end">
          <button
            onClick={onClose}
            className="text-sm text-gray-600 hover:text-gray-900 px-4 py-2 rounded-lg border transition-colors"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

// ---- main page ------------------------------------------------------------

export default function SessionPage() {
  const { id } = useParams() as { id: string };
  const router = useRouter();

  const [session, setSession] = useState<SessionState | null>(null);
  const [audit, setAudit] = useState<AuditTrail | null>(null);
  const [diagnosing, setDiagnosing] = useState(false);
  const [tier, setTier] = useState("auto");
  const [maxSteps, setMaxSteps] = useState(10);
  const [activeTab, setActiveTab] = useState<"evidence" | "audit" | "state">("evidence");
  const [showConfirm, setShowConfirm] = useState(false);
  const [error, setError] = useState("");

  const pendingTokenEntries = (session?.action_log ?? []).filter(
    (a) => a.status === "pending" && a.confirmation_token
  );

  const refresh = useCallback(async () => {
    try {
      const [s, a] = await Promise.all([api.getSession(id), api.getAudit(id)]);
      setSession(s);
      setAudit(a);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load session");
    }
  }, [id]);

  useEffect(() => { refresh(); }, [refresh]);

  async function runDiagnose() {
    setDiagnosing(true);
    setError("");
    try {
      await api.diagnose(id, tier, maxSteps);
      await refresh();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Diagnose failed");
    } finally {
      setDiagnosing(false);
    }
  }

  async function handleConfirm(token: string) {
    try {
      await api.confirm(id, token);
      await refresh();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Confirm failed");
    }
  }

  if (!session) {
    return <div className="text-sm text-gray-400">{error || "Loading session..."}</div>;
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <button
            onClick={() => router.push("/")}
            className="text-sm text-gray-400 hover:text-gray-600 mb-1 inline-flex items-center gap-1"
          >
            ← Sessions
          </button>
          <h1 className="text-xl font-bold font-mono">{id.slice(0, 8)}…</h1>
          <div className="flex items-center gap-2 mt-1 flex-wrap">
            <Badge label={session.os_platform} color="bg-gray-100 text-gray-600" />
            <Badge
              label={session.is_resolved ? "resolved" : session.loop_status}
              color={session.is_resolved ? "bg-green-100 text-green-700" : (STATUS_COLORS[session.loop_status] ?? "bg-gray-100 text-gray-600")}
            />
            <span className="text-xs text-gray-400">{session.loop_counter} steps</span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {pendingTokenEntries.length > 0 && (
            <button
              onClick={() => setShowConfirm(true)}
              className="text-sm bg-orange-500 hover:bg-orange-600 text-white px-4 py-2 rounded-lg font-medium transition-colors animate-pulse"
            >
              {pendingTokenEntries.length} Pending Approval
            </button>
          )}
          <button
            onClick={refresh}
            className="text-sm text-gray-500 hover:text-gray-700 border border-gray-200 px-3 py-2 rounded-lg transition-colors"
          >
            Refresh
          </button>
        </div>
      </div>

      {/* Symptoms */}
      <div className="bg-white rounded-xl border border-gray-200 p-4">
        <p className="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">Symptoms</p>
        <div className="flex flex-wrap gap-2">
          {session.symptoms.map((s, i) => (
            <span key={i} className="bg-gray-100 text-gray-700 text-sm rounded-full px-3 py-0.5">
              {s}
            </span>
          ))}
        </div>
        {session.user_description && (
          <p className="text-sm text-gray-500 mt-2 border-t pt-2">{session.user_description}</p>
        )}
      </div>

      {/* Diagnose control */}
      {!session.is_resolved && session.loop_status !== "success" && (
        <div className="bg-white rounded-xl border border-gray-200 p-4 flex items-center gap-4 flex-wrap">
          <div className="flex items-center gap-2">
            <label className="text-sm font-medium">Tier</label>
            <select
              value={tier}
              onChange={(e) => setTier(e.target.value)}
              className="border rounded-lg px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand/40"
            >
              {["auto", "tier0", "tier1", "tier2"].map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-sm font-medium">Max steps</label>
            <input
              type="number"
              min={1}
              max={20}
              value={maxSteps}
              onChange={(e) => setMaxSteps(Number(e.target.value))}
              className="border rounded-lg px-2 py-1.5 text-sm w-16 focus:outline-none focus:ring-2 focus:ring-brand/40"
            />
          </div>
          <button
            onClick={runDiagnose}
            disabled={diagnosing}
            className="bg-brand hover:bg-brand-dark disabled:opacity-50 text-white px-6 py-2 rounded-lg text-sm font-medium transition-colors"
          >
            {diagnosing ? "Diagnosing…" : "Run Diagnosis"}
          </button>
          {session.escalation_reason && (
            <span className="text-sm text-orange-600">⚠ {session.escalation_reason}</span>
          )}
        </div>
      )}

      {session.is_resolved && (
        <div className="bg-green-50 border border-green-200 rounded-xl p-4 text-sm text-green-700 font-medium">
          Session resolved successfully after {session.loop_counter} steps.
        </div>
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-600">
          {error}
        </div>
      )}

      {/* Tabs */}
      <div className="bg-white rounded-xl border border-gray-200">
        <div className="flex border-b border-gray-200">
          {(["evidence", "audit", "state"] as const).map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-5 py-3 text-sm font-medium capitalize transition-colors ${
                activeTab === tab
                  ? "border-b-2 border-brand text-brand"
                  : "text-gray-500 hover:text-gray-700"
              }`}
            >
              {tab === "evidence"
                ? `Evidence (${session.evidence.length})`
                : tab === "audit"
                ? `Audit Log (${session.action_log.length})`
                : "State"}
            </button>
          ))}
        </div>
        <div className="p-4">
          {activeTab === "evidence" && <EvidencePanel items={session.evidence} />}
          {activeTab === "audit" && <AuditViewer audit={audit} />}
          {activeTab === "state" && (
            <div className="grid grid-cols-2 gap-4 text-sm">
              {["device", "network", "cups", "windows"].map((key) => (
                <div key={key}>
                  <h4 className="font-medium capitalize mb-1">{key}</h4>
                  <pre className="bg-gray-50 rounded-lg p-3 text-xs overflow-auto max-h-48">
                    {JSON.stringify(session[key as keyof SessionState], null, 2)}
                  </pre>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {showConfirm && pendingTokenEntries.length > 0 && (
        <ConfirmationModal
          entries={pendingTokenEntries}
          onConfirm={handleConfirm}
          onClose={() => setShowConfirm(false)}
        />
      )}
    </div>
  );
}
