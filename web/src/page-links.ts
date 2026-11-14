// SPDX-License-Identifier: AGPL-3.0-only
// Page links that work at the site root (the local dev server) and under a sub-path (a build with base "/Pados/").
// Pages are joined to Vite's BASE_URL; an absolute "/operator.html" would leave the sub-path.

export type ConsolePage = "remote" | "operator";
export type RemoteViewId = "map" | "tasks" | "attention" | "actions";

const REMOTE_VIEW_IDS: readonly RemoteViewId[] = ["map", "tasks", "attention", "actions"];

function normalizedBase(base: string): string {
  const trimmed = (base || "/").trim();
  if (trimmed === "./" || trimmed === ".") return "./";
  const withLead = trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
  return withLead.endsWith("/") ? withLead : `${withLead}/`;
}

/** The page's URL under ``base``. The Remote console is the site's index page; the operator console is operator.html. */
export function pageHref(page: ConsolePage, base: string, query: Record<string, string> = {}): string {
  const params = new URLSearchParams(query).toString();
  const path = `${normalizedBase(base)}${page === "operator" ? "operator.html" : ""}`;
  return params ? `${path}?${params}` : path;
}

/** The Remote console view named by ``?view=`` (a deep link from the operator inbox), else the Work Map. */
export function remoteInitialView(search: string): RemoteViewId {
  const requested = new URLSearchParams(search).get("view");
  return REMOTE_VIEW_IDS.find((view) => view === requested) ?? "map";
}

export type InboxOpen = {
  surface: "REMOTE_CONSOLE" | "CONTROL_CENTER" | "API";
  view?: string;
  approval_id?: string;
  path?: string;
  needs?: string[];
};

export type InboxAction =
  | { kind: "link"; href: string; label: string; hint: string }
  | { kind: "in-console"; view: "approvals"; approvalId: string; label: string; hint: string }
  | { kind: "reference"; path: string; hint: string };

const NEEDS_TEXT: Record<string, string> = {
  REMOTE_SIGN_IN: "sign in to the Remote console (actor and access proof from PADOS_REMOTE_CONFIG)",
  ENROLLED_WEBAUTHN_DEVICE: "a WebAuthn device enrolled for that actor (Actions, then Register this device)",
  CONTROL_TOKEN: "the control token written by pados init",
};

function needsHint(needs: string[] | undefined): string {
  const parts = (needs ?? []).map((need) => NEEDS_TEXT[need] ?? need);
  return parts.length ? `Needs ${parts.join("; ")}.` : "";
}

/** Where a person acts on an inbox item. A result acceptance opens the Remote console's Actions view. */
export function inboxAction(open: InboxOpen | undefined, target: string, base: string): InboxAction {
  if (open?.surface === "REMOTE_CONSOLE") {
    return { kind: "link", href: pageHref("remote", base, { view: open.view || "actions" }),
      label: "Open in Remote console", hint: needsHint(open.needs) };
  }
  if (open?.surface === "CONTROL_CENTER" && open.approval_id) {
    return { kind: "in-console", view: "approvals", approvalId: open.approval_id, label: "Review",
      hint: needsHint(open.needs) };
  }
  return { kind: "reference", path: open?.path || target,
    hint: "No console page yet: use this API route with the control token, or the CLI." };
}
