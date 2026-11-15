// SPDX-License-Identifier: AGPL-3.0-only
// The Control Center's single sign-on section (sso.ts): the providers to sign in with, the message for a return
// from a provider, and, once signed in, who the actor is and its roles per project. Hidden when the server offers
// no provider and no actor session. The actor's CSRF token lives in this hook's state (page memory) only.
import { useCallback, useEffect, useRef, useState, type ReactElement } from "react";
import { formatDate } from "./api";
import {
  ROLE_LABELS,
  consumeSsoReturn,
  loadSsoView,
  logoutActor,
  ssoFailure,
  ssoReturnMessage,
  type SsoReturn,
  type SsoView,
} from "./sso";

export type Sso = {
  view: SsoView;
  /** The provider's redirect back, read once on load (the address bar is already clean). */
  returned: SsoReturn | null;
  notice: string;
  logout: () => Promise<void>;
};

// The provider's redirect back is read exactly once per page load (React may run state initialisers twice, and the
// operator console checks it before this section mounts), then the address bar is cleaned.
let pageReturn: { value: SsoReturn | null } | null = null;

export function pageSsoReturn(): SsoReturn | null {
  if (!pageReturn) pageReturn = { value: consumeSsoReturn() };
  return pageReturn.value;
}

export function useSso(): Sso {
  const [returned] = useState<SsoReturn | null>(pageSsoReturn);
  const [view, setView] = useState<SsoView>({ phase: "loading" });
  const [notice, setNotice] = useState("");
  const mounted = useRef(true);
  const sequence = useRef(0);

  const refresh = useCallback(async () => {
    const ticket = ++sequence.current;  // a late answer to an earlier read is dropped
    const next = await loadSsoView();
    if (mounted.current && ticket === sequence.current) setView(next);
  }, []);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => { mounted.current = false; };
  }, [refresh]);

  const logout = useCallback(async () => {
    if (view.phase !== "signed-in") return;
    try {
      await logoutActor(view.profile);
      setNotice("已登出單一登入工作階段。瀏覽器配對（若有）不受影響。");
    } catch (error) {
      setNotice(`登出失敗：${ssoFailure(error)}`);
    }
    await refresh();
  }, [view, refresh]);

  return { view, returned, notice, logout };
}

/** Whether the page was opened by a provider's redirect back (the operator console then opens this section). */
export function openedBySsoReturn(): boolean {
  return pageSsoReturn() !== null;
}

function ReturnNotice({ returned }: { returned: SsoReturn }): ReactElement {
  if (returned.kind === "ok") return <p className="control-status control-status-ok" role="status">{ssoReturnMessage(returned)}</p>;
  return <div className="control-error" role="alert">
    <p>{ssoReturnMessage(returned)}</p>
    <details><summary>技術細節（Details）</summary><p>錯誤代碼：<code>{returned.code}</code></p></details>
  </div>;
}

function Roles({ projects }: { projects: { projectId: string; roles: string[] }[] }): ReactElement {
  if (!projects.length) return <p className="control-muted">這個身分目前沒有任何專案角色。</p>;
  return <div className="control-table-wrap"><table className="control-table" aria-label="專案角色">
    <thead><tr><th>專案（Project）</th><th>角色（Roles）</th></tr></thead>
    <tbody>{projects.map((project) => <tr key={project.projectId}>
      <td><code>{project.projectId}</code></td>
      <td>{project.roles.map((role) => ROLE_LABELS[role] ?? role).join("、") || "—"}</td>
    </tr>)}</tbody>
  </table></div>;
}

type SectionProps = { view: SsoView; returned: SsoReturn | null; notice?: string; busy?: boolean; onLogout?: () => void };

/** The section itself: pure, so it renders the same in tests and in the page. */
export function SsoSection({ view, returned, notice = "", busy = false, onLogout }: SectionProps): ReactElement | null {
  const returnNotice = returned ? <ReturnNotice returned={returned} /> : null;
  const noticeLine = notice ? <p role="status">{notice}</p> : null;
  if (view.phase === "loading" || view.phase === "hidden") {
    // Nothing to offer; a return message (for example a refused sign-in) is still shown.
    return returnNotice || noticeLine ? <section className="control-card" aria-label="單一登入">{returnNotice}{noticeLine}</section> : null;
  }
  if (view.phase === "unavailable") {
    return <section className="control-card" aria-label="單一登入"><h2>單一登入（SSO）</h2>{returnNotice}
      <p className="control-muted">{view.reason}</p>{noticeLine}</section>;
  }
  const providers = view.providers.length
    ? <ul className="control-providers">{view.providers.map((provider) => <li key={provider.id}>
      <a className="control-button" href={provider.startUrl}>使用 {provider.label} 登入</a></li>)}</ul>
    : null;
  if (view.phase === "signed-out") {
    return <section className="control-card" aria-label="單一登入"><h2>單一登入（SSO）</h2>{returnNotice}
      <p className="control-muted">以組織帳號登入，以 PADOS 使用者身分建立工作請求。單一登入不會取代瀏覽器配對或控制權杖，
        也無法接受驗證結果（仍需在 Remote console 以 WebAuthn 驗證）。</p>
      {providers}{noticeLine}</section>;
  }
  const { profile } = view;
  return <section className="control-card" aria-label="單一登入"><h2>單一登入（SSO）</h2>{returnNotice}
    <div className="control-row"><p className="control-identity-status" role="status">
      已登入：<strong>{profile.displayName}</strong>（{profile.actorId}）</p>
      <button className="control-button" type="button" disabled={busy} onClick={onLogout}>登出單一登入</button></div>
    <dl className="control-facts">
      <dt>身分提供者</dt><dd>{profile.providerId || "—"}</dd>
      <dt>簽發者主機（issuer）</dt><dd><code>{profile.issuerHost}</code></dd>
      <dt>到期時間</dt><dd>{formatDate(profile.expiresAt)}</dd>
    </dl>
    <Roles projects={profile.projects} />
    <p className="control-muted">這些角色只適用於單一登入的工作請求；它們不授予控制權杖的權限，也不能接受驗證結果。</p>
    {noticeLine}</section>;
}

/** The section wired to the server: read on load, signed-in view from GET /identity/v1/me, actor-only logout. */
export function SsoPanel(): ReactElement | null {
  const sso = useSso();
  const [busy, setBusy] = useState(false);
  async function logout(): Promise<void> {
    setBusy(true);
    await sso.logout();
    setBusy(false);
  }
  return <SsoSection view={sso.view} returned={sso.returned} notice={sso.notice} busy={busy} onLogout={() => void logout()} />;
}
