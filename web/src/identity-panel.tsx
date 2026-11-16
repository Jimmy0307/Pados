// SPDX-License-Identifier: AGPL-3.0-only
// The Control Center's view of this browser's identity: who it is paired as, pairing it with a code from
// `pados device pair`, logging out, the paired devices, and the control token kept as the fallback. The session's
// CSRF token is held in this hook's state (page memory) only. When a session exists the token box is not shown; when
// pairing is off, unavailable or not done yet, the control token path stays exactly as it was.
import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactElement } from "react";
import { formatDate } from "./api";
import { ControlTokenPanel } from "./control-actions";
import { hasControlAuth, type ControlAuth } from "./control-auth";
import { HttpError } from "./http";
import { pageHref } from "./page-links";
import { SsoPanel } from "./sso-panel";
import {
  ENABLE_INSTRUCTION,
  PAIR_COMMAND,
  confirmAndRevoke,
  defaultDeviceLabel,
  identityFailure,
  identityStatusText,
  isCompletePairingCode,
  isSessionLost,
  listDevices,
  loadIdentityView,
  logout as logoutRequest,
  normalizePairingCode,
  pair as pairRequest,
  pairFailure,
  sessionEndedNotice,
  DEVICE_LABEL_MAX_LENGTH,
  type Device,
  type IdentityProvider,
  type IdentityView,
} from "./identity";

export type Identity = {
  view: IdentityView;
  notice: string;
  refresh: (notice?: string) => Promise<void>;
  /** Resolves to an operator-facing failure message, or null when this browser is now paired. */
  pair: (code: string, label: string) => Promise<string | null>;
  logout: () => Promise<string | null>;
  /** A control or identity call said the session is gone or refused: show why and read the session again. */
  sessionLost: (error: unknown) => void;
};

export function useIdentity(): Identity {
  const [view, setView] = useState<IdentityView>({ phase: "loading" });
  const [notice, setNotice] = useState("");
  const current = useRef<IdentityView>(view);
  const sequence = useRef(0);
  const mounted = useRef(true);

  const apply = useCallback((next: IdentityView) => { current.current = next; setView(next); }, []);

  const refresh = useCallback(async (explicit?: string) => {
    const ticket = ++sequence.current;  // a late answer to an earlier read is dropped
    const next = await loadIdentityView();
    if (!mounted.current || ticket !== sequence.current) return;
    const ended = sessionEndedNotice(current.current, next);
    // A session that could not be checked (no answer) is not a session that ended: keep it; its writes report for
    // themselves. Only an answer from the server (none, off, or another session) replaces it.
    if (!(next.phase === "unavailable" && current.current.phase === "paired")) apply(next);
    else if (explicit === undefined) return;
    if (explicit !== undefined) setNotice(explicit);
    else if (ended) setNotice(ended);
  }, [apply]);

  useEffect(() => {
    mounted.current = true;
    void refresh();  // restores a paired session on every load of the page
    return () => { mounted.current = false; };
  }, [refresh]);

  const pair = useCallback(async (code: string, label: string): Promise<string | null> => {
    try {
      const session = await pairRequest(code, label);
      if (!mounted.current) return null;
      const previous = current.current;
      sequence.current += 1;  // a read that started before pairing must not undo it
      apply({ phase: "paired", session, providers: previous.phase === "unpaired" ? previous.providers : [] });
      setNotice("");
      void refresh("");  // the server's own view (idle expiry) replaces the pairing answer
      return null;
    } catch (error) {
      const message = pairFailure(error);
      // An unknown outcome may have paired this browser; a disabled server changes what the page offers.
      if (error instanceof HttpError && (error.outcome === "WRITE_OUTCOME_UNKNOWN" || isSessionLost(error))) void refresh();
      return message;
    }
  }, [apply, refresh]);

  const logout = useCallback(async (): Promise<string | null> => {
    const active = current.current;
    if (active.phase !== "paired") return null;
    try {
      await logoutRequest(active.session);
      sequence.current += 1;  // the session is gone whatever an earlier read says
      apply({ phase: "unpaired", providers: active.providers });
      await refresh("Logged out: this browser's session was revoked. The device stays listed until it is revoked.");
      return null;
    } catch (error) {
      const message = identityFailure(error);
      if (isSessionLost(error) || (error instanceof HttpError && error.outcome === "WRITE_OUTCOME_UNKNOWN")) void refresh(message);
      return message;
    }
  }, [refresh]);

  const sessionLost = useCallback((error: unknown) => {
    const message = identityFailure(error);
    setNotice(message);  // shown at once, even if a later read takes over the refresh
    void refresh(message);
  }, [refresh]);

  return { view, notice, refresh, pair, logout, sessionLost };
}

/** The status line: "Paired as <label> (device <id>), expires <time>" or "Not paired". */
export function IdentityStatus({ view }: { view: IdentityView }): ReactElement {
  const idle = view.phase === "paired" && view.session.idleExpiresAt
    ? <small> · ends after inactivity at {formatDate(view.session.idleExpiresAt)}</small> : null;
  return <p className="control-identity-status" role="status">{identityStatusText(view, formatDate)}{idle}</p>;
}

export function PairForm({ onPair }: { onPair: (code: string, label: string) => Promise<string | null> }): ReactElement {
  const [code, setCode] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (busy || !isCompletePairingCode(code)) return;
    setBusy(true);
    setFailure("");
    const message = await onPair(code, label);
    setBusy(false);
    if (message) setFailure(message);
    else { setCode(""); setLabel(""); }
  }
  return <form className="control-form control-pair" aria-label="Pair this browser" onSubmit={(event) => void submit(event)}>
    <h3>Pair this browser</h3>
    <p className="control-muted">Run <code>{PAIR_COMMAND}</code> in a terminal on this computer, then type the one-time code
      it prints. The code works once, for 5 minutes. After pairing, reloading this page keeps you signed in; no control
      token is needed.</p>
    <label>Pairing code
      <input className="control-input" name="pairing-code" placeholder="XXXXX-XXXXX" autoComplete="off" spellCheck={false}
        autoCapitalize="characters" maxLength={11} value={code} onChange={(event) => setCode(normalizePairingCode(event.target.value))} /></label>
    <label>Label for this browser (optional)
      <input className="control-input" name="device-label" placeholder={defaultDeviceLabel()} autoComplete="off"
        maxLength={DEVICE_LABEL_MAX_LENGTH} value={label} onChange={(event) => setLabel(event.target.value)} /></label>
    <button className="control-button" type="submit" disabled={busy || !isCompletePairingCode(code)}>{busy ? "Pairing…" : "Pair this browser"}</button>
    {failure && <p className="control-error" role="alert">{failure}</p>}
  </form>;
}

function Providers({ providers }: { providers: IdentityProvider[] }): ReactElement | null {
  if (!providers.length) return null;
  // A pointer only: the sign-in links, the return message and the signed-in actor live in the SSO section of the
  // "This browser & devices" page (sso-panel.tsx).
  return <div className="control-providers"><p className="control-muted">Single sign-on is also offered on this server
    ({providers.map((provider) => provider.label).join(", ")}): see <strong>This browser &amp; devices</strong>.</p></div>;
}

type AccessProps = { identity: Identity; controlValue: string; onControlValue: (value: string) => void };

/** Shown above every page that can make a change: the session if there is one, else pairing and the token. */
export function ControlAccessPanel({ identity, controlValue, onControlValue }: AccessProps): ReactElement {
  const { view, notice } = identity;
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const noticeLine = notice ? <p className="control-error" role="alert">{notice}</p> : null;
  async function logout(): Promise<void> {
    setBusy(true);
    setFailure(await identity.logout() || "");
    setBusy(false);
  }
  if (view.phase === "loading") return <section className="control-card control-access"><IdentityStatus view={view} /></section>;
  if (view.phase === "paired") {
    return <section className="control-card control-access" aria-label="This browser">
      <div className="control-row"><IdentityStatus view={view} />
        <button className="control-button" type="button" disabled={busy} onClick={() => void logout()}>Log out</button></div>
      <p className="control-muted">Changes on this page use this browser's paired session; no control token is needed.</p>
      {(failure || notice) && <p className="control-error" role="alert">{failure || notice}</p>}
    </section>;
  }
  if (view.phase === "unpaired") {
    return <section className="control-card control-access" aria-label="This browser">
      <IdentityStatus view={view} />
      {noticeLine}
      <PairForm onPair={identity.pair} />
      <Providers providers={view.providers} />
      <details className="control-token-fallback" open={Boolean(controlValue)}>
        <summary>Use the control token instead</summary>
        <p className="control-muted">For when pairing is not possible: the token is kept in this page's memory only.</p>
        <ControlTokenPanel controlValue={controlValue} onChange={onControlValue} />
      </details>
    </section>;
  }
  // Pairing is off or unavailable: today's control token path, unchanged, with a word on how to turn pairing on.
  return <>
    {view.phase === "disabled"
      ? <p className="control-muted" role="note">Browser pairing is off. To pair this browser instead of entering the token on every
        load, {ENABLE_INSTRUCTION}.</p>
      : <p className="control-muted" role="note">{view.reason}</p>}
    {noticeLine}
    <ControlTokenPanel controlValue={controlValue} onChange={onControlValue} />
  </>;
}

export function DeviceList({ devices, busy, onRevoke }: { devices: Device[]; busy: boolean; onRevoke: (device: Device) => void }): ReactElement {
  if (!devices.length) return <p className="control-muted">No paired devices.</p>;
  return <div className="control-table-wrap"><table className="control-table" aria-label="Paired devices">
    <thead><tr><th>Device</th><th>Paired</th><th>Last seen</th><th>State</th><th /></tr></thead>
    <tbody>{devices.map((device) => <tr key={device.deviceId}>
      <td><strong>{device.deviceLabel}</strong>{device.current && <span className="control-status control-status-ok"> This browser</span>}<small>{device.deviceId}</small></td>
      <td>{formatDate(device.pairedAt)}</td>
      <td>{formatDate(device.lastSeenAt)}</td>
      <td>{device.revokedAt ? `Revoked ${formatDate(device.revokedAt)}` : "Active"}</td>
      <td>{!device.revokedAt && <button className="control-button" type="button" disabled={busy} onClick={() => onRevoke(device)}>Revoke</button>}</td>
    </tr>)}</tbody>
  </table></div>;
}

type DevicesProps = { auth: ControlAuth; onSessionLost: (error: unknown) => void; onCurrentRevoked: () => void };

export function DevicesPanel({ auth, onSessionLost, onCurrentRevoked }: DevicesProps): ReactElement {
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [generation, setGeneration] = useState(0);
  const usable = hasControlAuth(auth);
  useEffect(() => {
    if (!usable) return;
    let live = true;  // a late answer for an earlier session or token is dropped
    listDevices(auth).then((result) => { if (live) { setDevices(result); setNotice(""); } })
      .catch((error: unknown) => {
        if (!live) return;
        setNotice(identityFailure(error));
        if (isSessionLost(error)) onSessionLost(error);
      });
    return () => { live = false; };
  }, [auth, usable, generation, onSessionLost]);
  async function revoke(device: Device): Promise<void> {
    setBusy(true);
    try {
      if (await confirmAndRevoke(auth, device, (message) => window.confirm(message)) === "cancelled") return;
      setNotice(`Revoked ${device.deviceLabel} (${device.deviceId}).`);
      if (device.current) onCurrentRevoked();
      else setGeneration((value) => value + 1);
    } catch (error) {
      setNotice(`Not revoked: ${identityFailure(error)}`);
      if (isSessionLost(error)) onSessionLost(error);
      else if (error instanceof HttpError && (error.code === "DEVICE_NOT_FOUND" || error.outcome === "WRITE_OUTCOME_UNKNOWN")) {
        setGeneration((value) => value + 1);
      }
    } finally {
      setBusy(false);
    }
  }
  return <section className="control-card" aria-label="Devices"><div className="control-row"><h2>Devices</h2>
    {usable && <button className="control-button" type="button" disabled={busy} onClick={() => setGeneration((value) => value + 1)}>Reload</button>}</div>
    <p className="control-muted">Browsers paired with this PADOS. Revoking a device ends all of its sessions; logging out ends only this browser's session.</p>
    {!usable && <p className="control-muted">Pair this browser, or enter the control token above, to see the paired devices.</p>}
    {usable && !devices && !notice && <p className="control-muted" role="status">Loading devices…</p>}
    {usable && devices && <DeviceList devices={devices} busy={busy} onRevoke={(device) => void revoke(device)} />}
    {notice && <p role="status">{notice}</p>}
  </section>;
}

/** The "This browser" page: status, pairing, the token fallback, the devices, and what a session never does. */
export function IdentityPage({ identity, auth, controlValue, onControlValue }: AccessProps & { auth: ControlAuth }): ReactElement {
  const { view } = identity;
  const devicesOffered = view.phase === "paired" || view.phase === "unpaired";
  return <>
    <ControlAccessPanel identity={identity} controlValue={controlValue} onControlValue={onControlValue} />
    <SsoPanel />
    {devicesOffered && <DevicesPanel auth={auth} onSessionLost={identity.sessionLost}
      onCurrentRevoked={() => void identity.refresh("This browser's device was revoked: pair this browser again to make changes.")} />}
    <p className="control-muted">A paired session has exactly the control token's authority and never more: accepting a
      verified result always needs a fresh WebAuthn step-up in the <a href={pageHref("remote", import.meta.env.BASE_URL, { view: "actions" })}>Remote console</a>.
      A session also cannot mint pairing codes; only <code>{PAIR_COMMAND}</code> on this computer can.</p>
  </>;
}
