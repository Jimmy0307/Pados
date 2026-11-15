// SPDX-License-Identifier: AGPL-3.0-only
// Shared presentation of the product pages: the five states every step can be in (Loading, Disabled with its reason,
// Blocked with the next step, Failed in plain words with the technical code on demand, Completed), a status chip, and
// a Markdown preview that renders text and links as text only (no HTML is ever injected).
import type { ReactElement, ReactNode } from "react";
import { useT, type MessageKey } from "./i18n";
import { failureView, markdownBlocks, type InlinePart } from "./product-flow";

export function Loading({ label }: { label?: string }): ReactElement {
  const t = useT();
  return <div className="product-state product-state-loading" role="status">{label || t("state.loading")}</div>;
}

export function Disabled({ reason }: { reason: string }): ReactElement {
  const t = useT();
  return <div className="product-state product-state-disabled" role="note"><strong>{t("state.disabled")}</strong><span>{reason}</span></div>;
}

export function Blocked({ message, next, children }: { message: string; next?: string; children?: ReactNode }): ReactElement {
  const t = useT();
  return <div className="product-state product-state-blocked" role="status" data-state="blocked">
    <strong>{t("state.blocked")}</strong><span>{message}</span>
    {next && <span className="product-next"><b>{t("state.next")}:</b> {next}</span>}
    {children && <div className="product-actions">{children}</div>}
  </div>;
}

export function Failed({ error, onRetry }: { error: unknown; onRetry?: () => void }): ReactElement {
  const t = useT();
  const view = failureView(error);
  return <div className="product-state product-state-failed" role="alert" data-code={view.code}>
    <strong>{t("state.failed")}</strong><span>{t(view.key)}</span>
    <details><summary>{t("state.technical")}</summary><code>{view.code}</code>{view.status ? <small> HTTP {view.status}</small> : null}
      {view.requestId && <small> · request {view.requestId}</small>}</details>
    {onRetry && <button className="control-button" type="button" onClick={onRetry}>{t("state.retry")}</button>}
  </div>;
}

export function Completed({ message, children }: { message: string; children?: ReactNode }): ReactElement {
  const t = useT();
  return <div className="product-state product-state-completed" role="status" data-state="completed">
    <strong>{t("state.completed")}</strong><span>{message}</span>{children}
  </div>;
}

export function Chip({ value }: { value?: string | null }): ReactElement {
  const normalized = (value || "UNKNOWN").toUpperCase();
  const tone = /COMPLETED|PASSED|APPROVED|SUCCEEDED|ACCEPTED|APPROVE$|READY|VERIFIED|ACTIVE/.test(normalized) ? "ok"
    : /FAIL|REJECT|CANCEL|BLOCKED|TIMED_OUT|ERROR/.test(normalized) ? "error"
      : /WAIT|PENDING|PAUSED|REVISION|RUNNING|ROUTED|QUEUED/.test(normalized) ? "warning" : "unknown";
  return <span className={`control-status control-status-${tone}`}>{normalized}</span>;
}

export function Section({ title, children, testId }: { title: string; children: ReactNode; testId?: string }): ReactElement {
  return <section className="control-card product-section" data-testid={testId}><h2>{title}</h2>{children}</section>;
}

export function Empty({ messageKey }: { messageKey: MessageKey }): ReactElement {
  const t = useT();
  return <p className="product-empty">{t(messageKey)}</p>;
}

function Inline({ parts }: { parts: InlinePart[] }): ReactElement {
  return <>{parts.map((part, index) => part.kind === "text" ? <span key={index}>{part.text}</span>
    : <span key={index} className={`product-md-link${part.relative ? " product-md-relative" : ""}`} title={part.target}>{part.text} <small>({part.target})</small></span>)}</>;
}

export function MarkdownPreview({ source }: { source: string }): ReactElement {
  const blocks = markdownBlocks(source);
  return <div className="product-md-preview" data-testid="markdown-preview">
    {blocks.map((block, index) => {
      if (block.kind === "code") return <pre key={index}><code>{block.text}</code></pre>;
      if (block.kind === "heading") return <div key={index} className={`product-md-h product-md-h${block.level}`} role="heading" aria-level={block.level}><Inline parts={block.parts} /></div>;
      if (block.kind === "item") return <div key={index} className="product-md-item">• <Inline parts={block.parts} /></div>;
      return <p key={index}><Inline parts={block.parts} /></p>;
    })}
  </div>;
}
