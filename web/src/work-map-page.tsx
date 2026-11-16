// SPDX-License-Identifier: AGPL-3.0-only
// 工作心智圖 (Work Map) page: a mind map (SVG edges + positioned buttons) or a list of the tree that work-map.ts builds
// from GET /control/projects/{id}/work-map. Read-only: selecting, expanding and switching views change page memory only;
// creating work from a node opens the existing work request flow (工作需求 → 執行), which goes through the governed
// work-request and approval routes; a ResultCandidate decision links to the Remote console's WebAuthn step-up.
import { useMemo, useState, type ReactElement } from "react";
import { formatDate } from "./api";
import { hasControlAuth, type ControlAuth } from "./control-auth";
import { useT, type MessageKey, type Translate } from "./i18n";
import { pageHref } from "./page-links";
import { listProjects, workMap } from "./product-api";
import { useLoad } from "./product-load";
import { Chip, Disabled, Empty, Failed, Loading, Section } from "./product-ui";
import {
  LAYOUT, allIds, buildWorkTree, edgePath, expandedSet, layoutTree, searchNodes, selectNode, selectedNode, toggleNode,
  visibleRows, withExpanded, type MapMemory, type MapNode, type Text, type WorkMapData,
} from "./work-map";

export type OpenWork = (projectId: string, loopId?: string, draft?: { title: string; text: string }) => void;
type Props = { auth: ControlAuth; onSessionLost?: (error: unknown) => void; memory: MapMemory; onMemory: (memory: MapMemory) => void; onOpenWork: OpenWork };

export function textOf(t: Translate, text: Text): string {
  return "text" in text ? text.text : t(text.key, text.params);
}

export function WorkMapPage({ auth, onSessionLost, memory, onMemory, onOpenWork }: Props): ReactElement {
  const t = useT();
  const allowed = hasControlAuth(auth);
  const projects = useLoad(allowed ? () => listProjects(auth) : null, [auth, allowed], onSessionLost);
  const list = projects.data?.projects || [];
  const projectId = memory.projectId && list.some((p) => p.id === memory.projectId) ? memory.projectId : list[0]?.id || null;
  const work = useLoad(allowed && projectId ? () => workMap(auth, projectId) : null, [auth, allowed, projectId], onSessionLost);
  if (!allowed) return <Disabled reason={t("auth.required")} />;
  return <>
    <div className="control-heading"><div><span className="control-eyebrow">PADOS</span><h1>{t("map.title")}</h1><p>{t("map.detail")}</p></div></div>
    <p className="control-muted map-authority">{t("map.authority")}</p>
    {projects.loading && !projects.data && <Loading />}
    {Boolean(projects.error) && <Failed error={projects.error} onRetry={projects.reload} />}
    {projects.data && list.length === 0 && <Disabled reason={t("map.noProjects")} />}
    {list.length > 0 && <label className="map-project">{t("map.project")}
      <select className="control-input" name="map-project" value={projectId || ""} onChange={(e) => onMemory({ ...memory, projectId: e.target.value })}>
        {list.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>}
    {work.loading && !work.data && <Loading />}
    {Boolean(work.error) && <Failed error={work.error} onRetry={work.reload} />}
    {work.data && work.data.project.id === projectId && <WorkMapView data={work.data} memory={memory} onMemory={onMemory} onOpenWork={onOpenWork} onRefresh={work.reload} />}
  </>;
}

export function WorkMapView({ data, memory, onMemory, onOpenWork, onRefresh }: {
  data: WorkMapData; memory: MapMemory; onMemory: (memory: MapMemory) => void; onOpenWork: OpenWork; onRefresh?: () => void;
}): ReactElement {
  const t = useT();
  const root = useMemo(() => buildWorkTree(data), [data]);
  const [query, setQuery] = useState("");
  const expanded = expandedSet(memory, root);
  const selected = selectedNode(memory, root);
  const hits = searchNodes(root, query);
  const select = (id: string | null) => onMemory(selectNode(memory, root, id));
  const toggle = (id: string) => onMemory(toggleNode(memory, root, id));
  function search(value: string): void {
    setQuery(value);
    const found = searchNodes(root, value);
    if (found.length) select(found[0]);
  }
  return <div className="map-layout">
    <Section title={data.project.name} testId="work-map">
      <div className="map-toolbar" role="toolbar" aria-label={t("map.title")}>
        <div className="map-modes">
          {(["map", "list"] as const).map((mode) => <button key={mode} type="button" className={`control-button${memory.mode === mode ? " product-primary" : ""}`}
            aria-pressed={memory.mode === mode} onClick={() => onMemory({ ...memory, mode })}>{t(mode === "map" ? "map.mode.map" : "map.mode.list")}</button>)}
        </div>
        <button className="control-button" type="button" onClick={() => onMemory(withExpanded(memory, root.refs.projectId, allIds(root)))}>{t("map.expandAll")}</button>
        <button className="control-button" type="button" onClick={() => onMemory(withExpanded(memory, root.refs.projectId, [root.id]))}>{t("map.collapseAll")}</button>
        {onRefresh && <button className="control-button" type="button" onClick={onRefresh}>{t("map.refresh")}</button>}
        <label className="map-search">{t("map.search")}<input className="control-input" type="search" name="map-search" value={query} onChange={(e) => search(e.target.value)} /></label>
        {query && <small className="control-muted" data-testid="map-search-result">{hits.length ? t("map.searchHits", { count: hits.length }) : t("map.searchNone")}</small>}
      </div>
      {data.execution.truncated && <p className="control-muted">{t("map.truncated")}</p>}
      {root.children.length === 0 && <><Empty messageKey="map.empty" />
        <div className="product-actions"><button className="control-button product-primary" type="button" onClick={() => onOpenWork(root.refs.projectId)}>{t("map.createWork")}</button></div></>}
      {root.children.length > 0 && (memory.mode === "map"
        ? <MindMap root={root} expanded={expanded} selectedId={selected?.id || null} hits={new Set(hits)} onSelect={select} onToggle={toggle} />
        : <TreeList root={root} expanded={expanded} selectedId={selected?.id || null} hits={new Set(hits)} onSelect={select} onToggle={toggle} />)}
    </Section>
    <NodeDetail node={selected} onOpenWork={onOpenWork} />
  </div>;
}

type TreeProps = { root: MapNode; expanded: ReadonlySet<string>; selectedId: string | null; hits: ReadonlySet<string>; onSelect: (id: string) => void; onToggle: (id: string) => void };

function NodeLabel({ node }: { node: MapNode }): ReactElement {
  const t = useT();
  return <><small className="map-kind">{t(`map.kind.${node.kind}` as MessageKey)}</small>
    <span className="map-title">{textOf(t, node.title)}</span></>;
}

function MindMap({ root, expanded, selectedId, hits, onSelect, onToggle }: TreeProps): ReactElement {
  const t = useT();
  const layout = layoutTree(root, expanded);
  return <div className="map-canvas" data-testid="work-map-canvas">
    <div className="map-stage" style={{ width: layout.width, height: layout.height }}>
      <svg className="map-edges" width={layout.width} height={layout.height} aria-hidden="true">
        {layout.edges.map((edge) => <path key={`${edge.from.node.id}>${edge.to.node.id}`} d={edgePath(edge.from, edge.to)} />)}
      </svg>
      {layout.placed.map(({ node, x, y, expanded: open }) => <div key={node.id} data-node-id={node.id} data-kind={node.kind} data-state={node.state}
        className={`map-node map-kind-${node.kind} map-state-${node.state}${node.id === selectedId ? " map-selected" : ""}${hits.has(node.id) ? " map-hit" : ""}`}
        style={{ left: x, top: y, width: LAYOUT.nodeWidth, height: LAYOUT.nodeHeight }}>
        <button type="button" className="map-node-main" aria-pressed={node.id === selectedId} title={textOf(t, node.title)} onClick={() => onSelect(node.id)}>
          <NodeLabel node={node} /><span className="map-node-status">{node.status}{node.blocking ? " · !" : ""}</span>
        </button>
        {node.children.length > 0 && node !== root && <button type="button" className="map-toggle" aria-expanded={open}
          aria-label={t(open ? "map.collapse" : "map.expand", { name: textOf(t, node.title) })} onClick={() => onToggle(node.id)}>{open ? "−" : "+"}</button>}
      </div>)}
    </div>
  </div>;
}

function TreeList({ root, expanded, selectedId, hits, onSelect, onToggle }: TreeProps): ReactElement {
  const t = useT();
  return <ul className="map-list" role="tree" data-testid="work-map-list">
    {visibleRows(root, expanded).map(({ node, depth, expanded: open }) => <li key={node.id} role="treeitem" aria-level={depth + 1}
      aria-expanded={node.children.length ? open : undefined} aria-selected={node.id === selectedId} data-node-id={node.id} data-state={node.state}
      className={`map-row map-state-${node.state}${node.id === selectedId ? " map-selected" : ""}${hits.has(node.id) ? " map-hit" : ""}`} style={{ paddingLeft: depth * 18 }}>
      {node.children.length > 0 && depth > 0
        ? <button type="button" className="map-toggle" aria-label={t(open ? "map.collapse" : "map.expand", { name: textOf(t, node.title) })} onClick={() => onToggle(node.id)}>{open ? "−" : "+"}</button>
        : <span className="map-toggle-space" />}
      <button type="button" className="map-row-main" onClick={() => onSelect(node.id)}><NodeLabel node={node} /></button>
      <Chip value={node.status} />
    </li>)}
  </ul>;
}

function NodeDetail({ node, onOpenWork }: { node: MapNode | null; onOpenWork: OpenWork }): ReactElement {
  const t = useT();
  if (!node) return <Section title={t("map.details")} testId="work-map-detail"><p className="control-muted">{t("map.selectHint")}</p></Section>;
  const canCreate = node.kind === "project" || node.kind === "goal" || node.kind === "task";
  const draft = node.refs.draftTitle ? { title: node.refs.draftTitle, text: node.refs.draftText || node.refs.draftTitle } : undefined;
  const remote = pageHref("remote", import.meta.env.BASE_URL, { view: "actions" });
  return <Section title={t("map.details")} testId="work-map-detail">
    <p className="map-detail-kind"><small className="map-kind">{t(`map.kind.${node.kind}` as MessageKey)}</small></p>
    <h3 className="map-detail-title" data-testid="map-detail-title">{textOf(t, node.title)}</h3>
    <dl className="control-details map-detail">
      <div><dt>{t("map.status")}</dt><dd data-testid="map-detail-status"><Chip value={node.status} /></dd></div>
      <div><dt>{t("map.blocking")}</dt><dd data-testid="map-detail-blocking">{node.blocking ? textOf(t, node.blocking) : t("map.none")}</dd></div>
      <div><dt>{t("map.next")}</dt><dd data-testid="map-detail-next">{node.next ? textOf(t, node.next) : t("map.none")}</dd></div>
      <div><dt>{t("map.dependencies")}</dt><dd>{node.dependencies.length ? <ul className="map-plain">{node.dependencies.map((d, i) => <li key={i}>{textOf(t, d)}</li>)}</ul> : t("map.none")}</dd></div>
      <div><dt>{t("map.history")}</dt><dd data-testid="map-detail-history">{node.history.length
        ? <ul className="map-plain">{node.history.map((h, i) => <li key={i}>{textOf(t, h.title)} · <code>{h.status}</code> <small>{formatDate(h.at || undefined)}</small></li>)}</ul> : t("map.none")}</dd></div>
      {node.facts.map((fact) => <div key={fact.term}><dt>{t(fact.term)}</dt><dd><code className="map-fact">{fact.value}</code></dd></div>)}
    </dl>
    <div className="product-actions">
      {canCreate && <button className="control-button product-primary" type="button" onClick={() => onOpenWork(node.refs.projectId, undefined, draft)}>{t("map.createWork")}</button>}
      {node.refs.loopId && node.kind !== "evidence" && <button className="control-button" type="button" onClick={() => onOpenWork(node.refs.projectId, node.refs.loopId)}>{t("map.openRun")}</button>}
      {node.kind === "decision" && node.status === "WAITING_STEP_UP" && <a className="control-button" href={remote}>{t("map.openDecision")}</a>}
    </div>
  </Section>;
}
