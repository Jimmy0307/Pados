// SPDX-License-Identifier: AGPL-3.0-only
import type { ReactElement } from "react";
import { shortId, type EnterpriseVisibility } from "./api";

function Tag({ status }: { status: string }): ReactElement {
  return <span className={`control-status control-status-${status.toLowerCase()}`}>{status}</span>;
}

/** Minimal Enterprise extension visibility inside Mission Control (read-only projection). */
export function EnterprisePanel({ enterprise }: { enterprise?: EnterpriseVisibility }): ReactElement | null {
  if (!enterprise) return null;
  return <section className="control-card" aria-label="Enterprise capabilities">
    <div className="control-row"><h2>Enterprise capabilities</h2><Tag status={enterprise.status.replace(/^ENTERPRISE_EXTENSION_/, "")} /></div>
    <p className="control-muted">{enterprise.core.replace("_", " ")} · extension status never changes Core readiness · evidence is not truth</p>
    {enterprise.capabilities.map((item) => <div className="control-row" key={item.capability_id}>
      <span>{item.capability_id} · {item.capability_type} · {item.execution_mode} · max {item.max_classification}</span>
      <Tag status={item.health} />
      {item.reason && <small>{item.reason}</small>}
    </div>)}
    <div className="control-row"><strong>Recent evidence</strong><span>{enterprise.recent_evidence.length}</span></div>
    {enterprise.recent_evidence.slice(0, 5).map((item) => <div className="control-row" key={item.run_id}>
      <span>run {shortId(item.run_id)} · {item.provider_id} · {item.execution_class} · {item.source_ids.length} sources ({item.classifications.join(", ")})</span>
      <small>withheld {item.authorization.withheld_count} · ceiling {item.authorization.ceiling}</small>
    </div>)}
    <div className="control-row"><strong>Authorization failures</strong><span>{enterprise.authorization_failures.length}</span></div>
    {enterprise.authorization_failures.slice(0, 5).map((item, index) => <div className="control-row" key={`${item.kind}-${index}`}>
      <span>{item.kind}</span><small>{item.run_id ? `run ${shortId(item.run_id)}` : item.capability_id || ""} {item.count ? `· ${item.count}` : ""} {item.reason || ""}</small>
    </div>)}
  </section>;
}
