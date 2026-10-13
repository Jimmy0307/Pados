// SPDX-License-Identifier: AGPL-3.0-only
import type { ReactElement } from "react";
import { formatDate, shortId, type ComputeNodesVisibility } from "./api";

function Tag({ status }: { status: string }): ReactElement {
  return <span className={`control-status control-status-${status.toLowerCase()}`}>{status}</span>;
}

/** Minimal registered-node view inside Mission Control (no fleet management). */
export function ComputeNodesPanel({ computeNodes }: { computeNodes?: ComputeNodesVisibility }): ReactElement | null {
  if (!computeNodes || computeNodes.nodes.length === 0) return null;
  return <section className="control-card" aria-label="Local compute nodes">
    <div className="control-row"><h2>Local compute nodes</h2><span>{computeNodes.nodes.length}</span></div>
    <p className="control-muted">Execution substrate only · node results are accepted only through validation and CAS</p>
    {computeNodes.nodes.map((node) => <div key={node.node_id}>
      <div className="control-row">
        <strong>{node.node_name || node.node_id}</strong>
        <Tag status={node.stale ? `${node.state} (STALE)` : node.state} />
        {!node.authorized && <Tag status="UNAUTHORIZED" />}
      </div>
      <div className="control-row"><span>{node.platform || "—"} {node.architecture || ""} · {node.execution_class} · {node.capabilities.join(", ") || "no capabilities"}</span>
        <small>load {node.current_load ? `${node.current_load.running}/${node.current_load.max_concurrent_jobs} running, ${node.current_load.queued} queued` : "—"}</small></div>
      <div className="control-row"><span>models: {node.models.map((m) => `${m.model_id} (${m.availability})`).join(", ") || "none"}</span></div>
      <div className="control-row"><small>last successful check {formatDate(node.last_successful_check || undefined)}</small>
        <small>{node.last_execution ? `last execution run ${shortId(node.last_execution.run_id)} · ${node.last_execution.status}` : "no executions"}</small></div>
      {node.reason && <div className="control-row"><small>{node.reason}</small></div>}
    </div>)}
  </section>;
}
