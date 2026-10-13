// SPDX-License-Identifier: AGPL-3.0-only
import { useEffect, useMemo, useState, type ChangeEvent, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import {
  authorizeBatch,
  createBatch,
  getBatch,
  getCorpusStatus,
  getEvidence,
  ingestBatch,
  listBatches,
  listDocuments,
  listProjects,
  retryBatch,
  reviewBatch,
  scanBatch,
  searchCorpus,
  type ApiError,
  type Batch,
  type CorpusStatus,
  type Document,
  type Project,
  type SearchResult,
} from "./api";
import { getAuthorizationEligibility } from "./authorization";
import { ControlCenter } from "./control-center";
import "./styles.css";

function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [status, setStatus] = useState<CorpusStatus | null>(null);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [selectedBatch, setSelectedBatch] = useState<Batch | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [evidence, setEvidence] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  const [surface, setSurface] = useState<"operator" | "control">("operator");

  const activeProject = useMemo(() => projects.find((item) => item.id === projectId), [projects, projectId]);

  useEffect(() => {
    listProjects()
      .then((response) => {
        setProjects(response.projects);
        if (response.projects.length) setProjectId(response.projects[0].id);
      })
      .catch(setError);
  }, []);

  async function refresh() {
    if (!projectId) return;
    setError(null);
    const [statusResponse, batchResponse, documentResponse] = await Promise.all([
      getCorpusStatus(projectId),
      listBatches(projectId),
      listDocuments(projectId),
    ]);
    setStatus(statusResponse.corpus);
    setBatches(batchResponse.batches);
    setDocuments(documentResponse.documents);
    if (selectedBatch) {
      const refreshed = await getBatch(selectedBatch.batch_id);
      setSelectedBatch(refreshed.batch);
    }
  }

  useEffect(() => {
    setSelectedBatch(null);
    setResults([]);
    setEvidence(null);
    refresh().catch(setError);
  }, [projectId]);

  async function runAction(label: string, action: () => Promise<unknown>) {
    setBusy(label);
    setError(null);
    try {
      await action();
      await refresh();
      if (selectedBatch) setSelectedBatch((await getBatch(selectedBatch.batch_id)).batch);
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setBusy("");
    }
  }

  function onFiles(event: ChangeEvent<HTMLInputElement>) {
    setFiles(Array.from(event.target.files || []));
  }

  async function upload() {
    if (!projectId || !files.length) return;
    await runAction("Staging files…", async () => {
      const created = await createBatch(projectId, files);
      setSelectedBatch(created.batch);
      setFiles([]);
    });
  }

  async function selectBatch(batch: Batch) {
    setError(null);
    try {
      setSelectedBatch((await getBatch(batch.batch_id)).batch);
    } catch (caught) {
      setSelectedBatch(batch);
      setError(caught as ApiError);
    }
  }

  async function review() {
    if (!selectedBatch) return;
    await runAction("Saving review…", async () => {
      const response = await reviewBatch(selectedBatch.batch_id, (selectedBatch.files || []).map((item) => item.relative_path));
      setSelectedBatch(response.batch);
    });
  }

  async function authorize(ingestAfter: boolean) {
    if (!selectedBatch) return;
    await runAction(ingestAfter ? "Authorizing and ingesting…" : "Recording authorization…", async () => {
      await authorizeBatch(selectedBatch);
      if (ingestAfter) await ingestBatch(selectedBatch.batch_id);
    });
  }

  async function search() {
    if (!projectId || !query.trim()) return;
    await runAction("Searching…", async () => {
      const response = await searchCorpus(projectId, query.trim());
      setResults(response.results);
    });
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">PADOS / LOCAL OPERATOR</p>
          <h1>Corpus workspace</h1>
        </div>
        <div className="runtime-actions"><button className={surface === "operator" ? "surface-active" : ""} onClick={() => setSurface("operator")}>Corpus Operator</button><button className={surface === "control" ? "surface-active" : ""} onClick={() => setSurface("control")}>Control Center</button><div className="runtime-pill"><span className="pulse" /> localhost only · {status?.embedding_policy || "KEYWORD_ONLY"}</div></div>
      </header>

      {surface === "operator" ? (
      <main className="workspace">
        <aside className="sidebar panel">
          <label className="field-label" htmlFor="project">Project context</label>
          <select id="project" value={projectId} onChange={(event) => setProjectId(event.target.value)}>
            {!projects.length && <option value="">Loading projects…</option>}
            {projects.map((project) => <option key={project.id} value={project.id}>{project.name || project.slug || project.id}</option>)}
          </select>
          <div className="side-note">
            <strong>Authority boundary</strong>
            <p>Operational projects are read-only authority. Corpus writes use the dedicated Corpus database and a backend receipt.</p>
          </div>
          <div className="side-note compact">
            <span className="status-dot" /> Approved root boundary active
          </div>
        </aside>

        <section className="content">
          {error && <div className="alert error" role="alert"><strong>{error.code || "Request failed"}</strong> {error.message}</div>}
          {busy && <div className="alert progress" role="status">{busy}</div>}
          <section className="hero panel">
            <div>
              <p className="eyebrow">CONTROLLED INGESTION</p>
              <h2>{activeProject?.name || "Select a project"}</h2>
              <p>Stage, inspect, and authorize document batches with explicit human control.</p>
            </div>
            <div className="hero-badge">TEXT-FIRST PDF<br /><span>OCR disabled</span></div>
          </section>

          <section className="metrics-grid">
            <Metric label="Documents" value={status?.counts.documents} />
            <Metric label="Pages" value={status?.counts.pages} />
            <Metric label="Chunks" value={status?.counts.chunks} />
            <Metric label="Evidence spans" value={status?.counts.evidence_spans} />
            <Metric label="Pending review" value={status?.pending_review_batches} accent />
          </section>

          <div className="two-column">
            <section className="panel upload-panel">
              <div className="section-heading"><div><p className="eyebrow">01 / STAGE</p><h3>Add documents</h3></div><span className="ceiling">100 files max</span></div>
              <label className="dropzone" htmlFor="files">
                <span className="upload-icon">＋</span>
                <strong>{files.length ? `${files.length} PDF file${files.length === 1 ? "" : "s"} selected` : "Choose PDF files"}</strong>
                <span>Files are copied into a project-scoped approved staging root.</span>
                <input id="files" type="file" accept="application/pdf,.pdf" multiple onChange={onFiles} />
              </label>
              {files.length > 0 && <div className="file-list">{files.map((file) => <span key={`${file.name}-${file.size}`}>{file.name}<small>{formatBytes(file.size)}</small></span>)}</div>}
              <button className="button primary" disabled={!files.length || !!busy} onClick={upload}>Stage selected files</button>
              <p className="microcopy">Server verifies MIME, filename, size, path, and SHA-256. Source files are never modified.</p>
            </section>

            <section className="panel policy-panel">
              <div className="section-heading"><div><p className="eyebrow">POLICY LOCK</p><h3>Review before authorization</h3></div><span className="lock">LOCKED</span></div>
              <PolicyRow label="Parser" value="TEXT_FIRST_PYPDF" />
              <PolicyRow label="Retrieval" value="KEYWORD_ONLY" />
              <PolicyRow label="OCR / cloud models" value="DISABLED" />
              <PolicyRow label="Operational migration" value="DISABLED" />
              <PolicyRow label="Write target" value={status?.database || "dedicated Corpus DB"} />
              <p className="microcopy">Authorization is a real operator decision. Nothing ingests automatically after staging or review.</p>
            </section>
          </div>

          <section className="panel batch-panel">
            <div className="section-heading"><div><p className="eyebrow">02 / BATCH QUEUE</p><h3>Batch state</h3></div><button className="button ghost" disabled={!!busy} onClick={() => refresh().catch(setError)}>Refresh</button></div>
            <div className="table-wrap"><table><thead><tr><th>Batch</th><th>State</th><th>Files</th><th>Idempotency</th><th /></tr></thead><tbody>
              {batches.map((batch) => <tr key={batch.batch_id} className={selectedBatch?.batch_id === batch.batch_id ? "selected-row" : ""} onClick={() => selectBatch(batch)}><td><code>{batch.batch_id.slice(0, 8)}</code></td><td><StateBadge state={batch.status} /></td><td>{batch.files?.length ?? (batch.database_record ? "existing" : 0)}</td><td>{batch.idempotency || "—"}</td><td><button className="link-button" onClick={(event) => { event.stopPropagation(); selectBatch(batch); }}>Inspect</button></td></tr>)}
              {!batches.length && <tr><td colSpan={5} className="empty">No batches in this project.</td></tr>}
            </tbody></table></div>
            {selectedBatch && <BatchInspector batch={selectedBatch} busy={!!busy} onScan={() => runAction("Scanning…", () => scanBatch(selectedBatch.batch_id))} onReview={review} onCancel={() => setSelectedBatch(null)} onAuthorize={() => authorize(false)} onAuthorizeIngest={() => authorize(true)} onIngest={() => runAction("Ingesting…", () => ingestBatch(selectedBatch.batch_id))} onRetry={() => runAction("Retrying…", () => retryBatch(selectedBatch.batch_id))} />}
          </section>

          <section className="panel search-panel">
            <div className="section-heading"><div><p className="eyebrow">03 / RETRIEVE</p><h3>Grounded search</h3></div><span className="ceiling">deterministic</span></div>
            <form className="search-bar" onSubmit={(event) => { event.preventDefault(); search().catch(setError); }}><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search approved evidence…" aria-label="Search approved evidence" /><button className="button primary" disabled={!query.trim() || !!busy}>Search</button></form>
            <div className="results">{results.map((result) => <article className="result-card" key={result.evidence_span_id}><div className="result-meta"><span>{result.source_filename}</span><span>p. {result.page_number}</span><button className="link-button" onClick={() => getEvidence(result.evidence_span_id).then((response) => setEvidence(response.evidence)).catch(setError)}>Open evidence</button></div><p>{result.matched_excerpt}</p><small>{String(result.match_explanation.method)} · evidence {result.evidence_span_id.slice(0, 8)}</small></article>)}{query && !results.length && <p className="empty">No grounded evidence matched this keyword.</p>}</div>
          </section>

          <section className="panel documents-panel">
            <div className="section-heading"><div><p className="eyebrow">CORPUS</p><h3>Documents</h3></div><span className="ceiling">project scoped</span></div>
            <div className="document-grid">{documents.map((document) => <div className="document-card" key={document.id}><span className="doc-type">PDF</span><strong>{document.canonical_title || "Untitled document"}</strong><small>{document.pages || 0} pages · {document.chunks || 0} chunks</small><StateBadge state={document.status || "PUBLISHED"} /></div>)}{!documents.length && <p className="empty">No documents in this project.</p>}</div>
          </section>
        </section>
      </main>
      ) : (
        <ControlCenter onBack={() => setSurface("operator")} />
      )}
      {evidence && <div className="drawer-backdrop" role="presentation" onClick={() => setEvidence(null)}><aside className="evidence-drawer" role="dialog" aria-modal="true" aria-label="Evidence detail" onClick={(event) => event.stopPropagation()}><div className="section-heading"><div><p className="eyebrow">EVIDENCE ANCHOR</p><h3>Source detail</h3></div><button className="icon-button" onClick={() => setEvidence(null)} aria-label="Close evidence">×</button></div><pre>{JSON.stringify(evidence, null, 2)}</pre></aside></div>}
    </div>
  );
}

function Metric({ label, value, accent = false }: { label: string; value?: number; accent?: boolean }) {
  return <div className={`metric panel ${accent ? "accent" : ""}`}><span>{label}</span><strong>{value ?? "—"}</strong></div>;
}

function PolicyRow({ label, value }: { label: string; value: string }) {
  return <div className="policy-row"><span>{label}</span><strong>{value}</strong></div>;
}

function StateBadge({ state }: { state: string }) {
  return <span className={`state state-${state.toLowerCase().replaceAll("_", "-")}`}>{state.replaceAll("_", " ")}</span>;
}

function BatchInspector({ batch, busy, onScan, onReview, onCancel, onAuthorize, onAuthorizeIngest, onIngest, onRetry }: { batch: Batch; busy: boolean; onScan: () => void; onReview: () => void; onCancel: () => void; onAuthorize: () => void; onAuthorizeIngest: () => void; onIngest: () => void; onRetry: () => void }) {
  const files = batch.files || [];
  const canReview = batch.status === "SCANNED" || batch.status === "PENDING_HUMAN_AUTHORIZATION";
  const authorization = getAuthorizationEligibility(batch, busy);
  const canIngest = batch.status === "AUTHORIZED" || batch.status === "FAILED" || batch.status === "INGESTING";
  return <div className="inspector"><div className="inspector-heading"><div><span className="eyebrow">SELECTED BATCH</span><h4>{batch.batch_id}</h4></div><StateBadge state={batch.status} /></div><div className="batch-files">{files.map((file) => <div key={file.relative_path}><span>{file.filename}</span><small>{file.duplicate_status} · {file.sha256.slice(0, 12)}…</small></div>)}</div><div className="action-row"><button className="button ghost" disabled={busy || batch.status === "PUBLISHED"} onClick={onScan}>Scan</button><button className="button ghost" disabled={busy || !canReview} onClick={onReview}>Review</button><button className="button ghost" disabled={busy || !canReview} onClick={onCancel}>Cancel</button><button className="button secondary" disabled={!authorization.enabled} onClick={onAuthorize}>Authorize</button><button className="button primary" disabled={!authorization.enabled} onClick={onAuthorizeIngest}>Authorize and Ingest</button><button className="button ghost" disabled={busy || !canIngest} onClick={batch.status === "FAILED" ? onRetry : onIngest}>{batch.status === "FAILED" ? "Retry" : "Ingest"}</button></div>{authorization.reasons.length > 0 && <ul className="microcopy" aria-label="Authorization disabled reasons">{authorization.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>}<p className="microcopy">The two authorization buttons are explicit human actions. Cancel leaves this batch pending; no background worker will ingest it.</p></div>;
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function Root() : ReactNode {
  return <App />;
}

createRoot(document.getElementById("root")!).render(<Root />);
