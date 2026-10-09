// SPDX-License-Identifier: AGPL-3.0-only
import { useCallback, useEffect, useState, type FormEvent, type ReactElement } from "react";
import { createRoot } from "react-dom/client";
import { RemoteApiError, RemoteClient, deriveBanner, type AttentionProjection, type ConnectivityProjection, type RemoteNodes, type RemoteRun, type WorkMapProjection } from "./remote-api";
import { ActionsView, AttentionView, ConnectivityBanner, REMOTE_VIEWS, TaskAssignmentView, WorkMapView, type RemoteView } from "./remote-console";
import "./remote.css";

function RemoteApp(): ReactElement {
  // Sign-in values live in memory only and are not written to browser storage.
  const [client, setClient] = useState<RemoteClient | null>(null);
  const [endpoint, setEndpoint] = useState("");
  const [endpointConfirmed, setEndpointConfirmed] = useState(false);
  const [view, setView] = useState<RemoteView>("map");
  const [attention, setAttention] = useState<AttentionProjection | null>(null);
  const [workMap, setWorkMap] = useState<WorkMapProjection | null>(null);
  const [nodes, setNodes] = useState<RemoteNodes | undefined>();
  const [notice, setNotice] = useState("");
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [reachable, setReachable] = useState(true);
  const [browserOnline, setBrowserOnline] = useState(typeof navigator === "undefined" ? true : navigator.onLine);
  const [connectivity, setConnectivity] = useState<ConnectivityProjection | null>(null);
  const [lastSync, setLastSync] = useState<string | undefined>();

  // Reconnect = authenticate + fetch current authoritative projections.
  // Browser history is never used to reconstruct job state.
  const refresh = useCallback(async () => {
    if (!client) return;
    try {
      const [nextAttention, nextWorkMap, nextNodes] = await Promise.all([
        client.attention(), client.workMap(), client.nodes(),
      ]);
      setAttention(nextAttention);
      setWorkMap(nextWorkMap);
      setNodes(nextNodes);
      // Optional projection: an older or unconfigured Home Base simply has none.
      setConnectivity(await client.connectivity().catch(() => null));
      setReachable(true);
      setNotice("");
      setLastSync(new Date().toLocaleTimeString());
    } catch (error) {
      if (error instanceof RemoteApiError) {
        setReachable(true);
        setNotice(error.detail);
      } else {
        setReachable(false);
      }
    }
  }, [client]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    const online = () => { setBrowserOnline(true); void refresh(); };
    const offline = () => setBrowserOnline(false);
    window.addEventListener("online", online);
    window.addEventListener("offline", offline);
    return () => { window.removeEventListener("online", online); window.removeEventListener("offline", offline); };
  }, [refresh]);
  useEffect(() => {
    if (!client || reachable) return;
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [client, reachable, refresh]);

  function signIn(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const remoteEndpoint = String(data.get("endpoint") || "");
    if (!endpointConfirmed) {
      setNotice("Confirm that the selected origin is your trusted PADOS Home Base before sending access proof.");
      return;
    }
    try {
      const next = new RemoteClient(
        { actor: String(data.get("actor") || ""), proof: String(data.get("proof") || "") },
        undefined,
        remoteEndpoint,
      );
      setEndpoint(remoteEndpoint);
      setClient(next);
      setNotice("");
    } catch {
      setNotice("Enter an HTTPS Home Base URL (HTTP is allowed only on localhost).");
    }
  }

  async function act(label: string, run: () => Promise<{ outcome: string }>): Promise<{ outcome: string } | null> {
    try {
      const result = await run();
      // Refresh first: a successful refresh clears notices, and the outcome must stay visible.
      await refresh();
      setNotice(`${label}: ${result.outcome}`);
      return result;
    } catch (error) {
      setNotice(`${label}: ${error instanceof RemoteApiError ? error.detail : "request failed"}`);
      return null;
    }
  }

  async function createTask(project: string, intent: string, priority: "LOW" | "NORMAL" | "HIGH", deadline?: string): Promise<boolean> {
    if (!client) return false;
    return (await act("create task", () => client.startTask(project, intent, priority, deadline))) !== null;
  }

  if (!client) {
    return <main className="rc-shell rc-signin-shell"><p className="rc-eyebrow">PERSONAL COGNITIVE & EXECUTION SYSTEM</p><h1>PADOS</h1>
      <p className="rc-intro">Connect this control surface to your PADOS Home Base.</p>
      <form className="rc-signin" onSubmit={signIn} aria-label="Sign in">
        <label>Home Base URL <span className="rc-optional">Blank uses this host</span><input name="endpoint" type="url" placeholder="https://pados.example.com" value={endpoint} onChange={(event) => { setEndpoint(event.target.value); setEndpointConfirmed(false); }} /></label>
        <label className="rc-origin-confirm"><input aria-label="Confirm Home Base origin" type="checkbox" checked={endpointConfirmed} onChange={(event) => setEndpointConfirmed(event.target.checked)} />
          I trust {endpoint || window.location.origin} to receive this access proof.</label>
        <label>Actor<input name="actor" autoComplete="username" required /></label>
        <label>Access proof<input name="proof" type="password" autoComplete="current-password" required /></label>
        <button type="submit">Connect</button>
      </form>
      {notice && <p className="rc-notice" role="alert">{notice}</p>}
      <p className="rc-note">Access values stay in memory for this session. The browser sends intent only.</p>
    </main>;
  }

  // Every decision runs a fresh WebAuthn step-up bound to the exact candidate the server presents.
  const onDecide = (run: RemoteRun, decision: "approve" | "reject") => void act(decision, () => client.decide(
    run, decision, undefined,
    (binding) => window.confirm(
      `${binding.action} ${binding.kind === "RESULT_CANDIDATE" ? "result" : "run"} ${binding.candidate_id}\n` +
      `digest ${binding.candidate_digest.slice(0, 16)}… · project ${binding.project_id}\n` +
      `classification ${binding.classification} · ScopeVersion ${binding.expected_scope_version}\n\n` +
      "Your authenticator will ask you to verify it is you.")));
  const onEnroll = () => void (async () => {
    try {
      const descriptor = await client.presenceDescriptor("pados-remote-device");
      setNotice(`Device descriptor (give to the Home Base operator to enrol): ${JSON.stringify(descriptor)}`);
    } catch (error) {
      setNotice(`register device: ${error instanceof RemoteApiError ? error.detail : "WebAuthn unavailable"}`);
    }
  })();

  return <main className="rc-shell rc-product-shell">
    <header className="rc-header"><div><p className="rc-eyebrow">PERSONAL COGNITIVE & EXECUTION SYSTEM</p><h1>PADOS</h1></div>
      <div className="rc-header-actions"><span className="rc-endpoint-label">{endpoint || "This host"}</span><button type="button" onClick={() => void refresh()}>Refresh</button></div></header>
    <ConnectivityBanner banner={deriveBanner({ browserOnline, reachable, connectivity })} lastSync={lastSync} />
    {notice && <p className="rc-notice" role="status">{notice}</p>}
    {!reachable && <p className="rc-offline-message" role="alert">OFFLINE · REMOTE UNREACHABLE. No action was reported as successful.</p>}
    {attention && workMap && <>
      {view === "map" && <WorkMapView projection={workMap} selectedId={selectedNode} onSelect={setSelectedNode} nodes={nodes} />}
      {view === "tasks" && <TaskAssignmentView tasks={workMap.tasks} projects={workMap.projects} onCreate={createTask} />}
      {view === "attention" && <AttentionView view={attention} />}
      {view === "actions" && <ActionsView view={attention} onDecide={onDecide} onEnroll={onEnroll} />}
    </>}
    <nav className="rc-tabs" aria-label="PADOS views">{REMOTE_VIEWS.map((item) =>
      <button key={item.id} type="button" aria-current={view === item.id ? "page" : undefined} onClick={() => setView(item.id)}>{item.label}</button>)}</nav>
  </main>;
}

if ("serviceWorker" in navigator && window.isSecureContext) {
  void navigator.serviceWorker.register(`${import.meta.env.BASE_URL}remote-sw.js`).catch(() => undefined);
}
createRoot(document.getElementById("root") as HTMLElement).render(<RemoteApp />);
