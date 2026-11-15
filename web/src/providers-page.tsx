// SPDX-License-Identifier: AGPL-3.0-only
// AI 模型與連線 (Providers): every configured local runtime and enterprise gateway with two separate status levels:
// 第一層 連線狀態 (reachable or not) and 第二層 治理狀態 per model (已授權並測量 / 已測量但未授權 / 未授權). Buttons test the
// connection, enable or disable a provider and edit its non-sensitive settings (never a key value). Every call is a /control route; the
// server decides and nothing here can make a model usable.
import { useState, type FormEvent, type ReactElement } from "react";
import { formatDate } from "./api";
import { hasControlAuth, type ControlAuth } from "./control-auth";
import { isSessionLost } from "./identity";
import { useT, type MessageKey } from "./i18n";
import { diagnoseProvider, listProviders, toggleProvider, updateProvider, type Provider, type ProviderCheck, type ProviderModel, type ProvidersOverview, type ProviderUpdate } from "./providers-api";
import { CLASSIFICATIONS, REQUIREMENTS, codeKey, formFrom, governanceKey, governanceTone, governedSummary, reachKey, reachTone, shortDigest, updateFrom, type ProviderForm } from "./providers-view";
import { useLoad } from "./product-load";
import { Completed, Disabled, Empty, Failed, Loading, Section } from "./product-ui";

type Access = { auth: ControlAuth; onSessionLost?: (error: unknown) => void };

function Tone({ tone, text, testId }: { tone: string; text: string; testId?: string }): ReactElement {
  return <span className={`control-status control-status-${tone}`} data-testid={testId}>{text}</span>;
}

function Reason({ code }: { code: string | null | undefined }): ReactElement | null {
  const t = useT();
  const key = codeKey(code);
  if (!code || !key) return null;
  return <span className="provider-reason"><span>{t(key)}</span><details><summary>{t("state.technical")}</summary><code>{code}</code></details></span>;
}

export function ProvidersPage({ auth, onSessionLost }: Access): ReactElement {
  const t = useT();
  const overview = useLoad(hasControlAuth(auth) ? () => listProviders(auth) : null, [auth], onSessionLost);
  if (!hasControlAuth(auth)) return <Disabled reason={t("auth.required")} />;
  return <>
    <div className="control-heading"><div><span className="control-eyebrow">PADOS</span><h1>{t("providers.title")}</h1><p>{t("providers.detail")}</p></div>
      <button className="control-button" type="button" onClick={overview.reload}>{t("state.retry")}</button></div>
    <section className="control-card provider-rule" role="note" data-testid="governed-use-rule"><h2>{t("providers.rule.title")}</h2><p>{t("providers.rule.body")}</p></section>
    {overview.loading && !overview.data && <Loading />}
    {Boolean(overview.error) && <Failed error={overview.error} onRetry={overview.reload} />}
    {overview.data && overview.data.providers.length === 0 && <Empty messageKey="providers.empty" />}
    {overview.data && overview.data.providers.map((provider) => <ProviderCard key={provider.provider_id} auth={auth} onSessionLost={onSessionLost}
      provider={provider} revision={overview.data?.setup_revision ?? 0} onChanged={overview.reload} />)}
    {overview.data && <CatalogAndAuthorizations overview={overview.data} />}
  </>;
}

export function ProviderCard({ auth, onSessionLost, provider: initial, revision, onChanged }: Access & { provider: Provider; revision: number; onChanged: () => void }): ReactElement {
  const t = useT();
  const [provider, setProvider] = useState(initial);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [editing, setEditing] = useState(false);
  const [saved, setSaved] = useState<number | null>(null);
  async function act(label: string, action: () => Promise<Provider | null>): Promise<void> {
    setBusy(label);
    setError(null);
    try {
      const next = await action();
      if (next) setProvider(next);
    } catch (failure) {
      setError(failure);
      if (isSessionLost(failure)) onSessionLost?.(failure);
    } finally {
      setBusy("");
    }
  }
  const gateway = provider.kind === "enterprise_gateway";
  const summary = governedSummary(provider);
  const reach = provider.reachability;
  return <section className="control-card provider-card" data-testid={`provider-${provider.provider_id}`} aria-label={provider.display_name}>
    <div className="control-row"><h2>{provider.display_name}</h2><span className="control-muted">{provider.kind} · {t(provider.source === "setup" ? "providers.source.setup" : "providers.source.settings")}</span></div>
    <div className="provider-levels">
      <div><span className="control-eyebrow">{t("providers.level.reach")}</span>
        <Tone tone={reachTone(reach.state)} text={t(reachKey(reach.state))} testId="reach-state" />
        {reach.runtime_version && <small> {t("providers.runtimeVersion", { version: reach.runtime_version })}</small>}
        <Reason code={reach.code} /></div>
      <div><span className="control-eyebrow">{t("providers.level.governance")}</span>
        <Tone tone={summary === "authorized" ? "ok" : summary === "none" ? "error" : "unknown"} text={t(`providers.summary.${summary}`)} testId="governance-summary" /></div>
    </div>
    <AuthLine provider={provider} />
    {provider.gateway?.contract === "OK" && <p className="control-muted">{provider.gateway.base_url} · {t("providers.policy", {
      classification: provider.gateway.contract_max_classification || "PUBLIC",
      execution: t(provider.gateway.contract_execution_enabled ? "providers.policy.on" : "providers.policy.off") })}</p>}
    {provider.circuit && <p className="control-muted" data-testid="circuit-state">{t("providers.circuit")}: {t(`providers.circuit.${provider.circuit.state}`)}
      {provider.circuit.retry_in_seconds !== null && ` · ${t("providers.circuit.retry", { seconds: provider.circuit.retry_in_seconds })}`}</p>}
    {provider.checked_at && <p className="control-muted">{t("providers.lastChecked", { time: formatDate(provider.checked_at) })}</p>}
    <div className="product-actions">
      <button className="control-button" type="button" disabled={Boolean(busy) || !provider.enabled}
        onClick={() => void act("test", async () => (await diagnoseProvider(auth, provider.provider_id, false)).provider)}>{busy === "test" ? t("providers.testing") : t("providers.test")}</button>
      {gateway && <button className="control-button" type="button" disabled={Boolean(busy) || !provider.enabled} title={t("providers.testProbe.help")}
        onClick={() => void act("probe", async () => (await diagnoseProvider(auth, provider.provider_id, true)).provider)}>{busy === "probe" ? t("providers.testing") : t("providers.testProbe")}</button>}
      <button className="control-button" type="button" disabled={Boolean(busy)}
        onClick={() => void act("toggle", async () => { const r = await toggleProvider(auth, provider.provider_id, !provider.enabled, revision); onChanged(); return r.provider; })}>
        {provider.enabled ? t("providers.disable") : t("providers.enable")}</button>
      <button className="control-button" type="button" disabled={Boolean(busy)} onClick={() => { setEditing(!editing); setSaved(null); }}>{t("providers.edit")}</button>
    </div>
    {gateway && <p className="control-muted">{t("providers.testProbe.help")}</p>}
    {Boolean(error) && <Failed error={error} />}
    {saved !== null && <Completed message={t("providers.saved", { revision: saved })} />}
    {editing && <EditForm provider={provider} onSubmit={(update) => act("save", async () => {
      const response = await updateProvider(auth, provider.provider_id, update, revision);
      setSaved(response.setup_revision);
      setEditing(false);
      onChanged();
      return response.provider;
    })} busy={busy === "save"} />}
    <Models models={provider.models} />
    <Checks checks={provider.checks} />
  </section>;
}

function AuthLine({ provider }: { provider: Provider }): ReactElement {
  const t = useT();
  const name = provider.gateway?.auth_env ?? provider.auth_env;
  const present = provider.gateway ? provider.gateway.auth_env_present : provider.auth_env_present;
  if (!name) return <p className="control-muted">{t("providers.authEnv")}: {t("providers.authEnv.none")}</p>;
  return <p className="control-muted" data-testid="auth-env">{t("providers.authEnv")}: <code>{name}</code> · {t(present ? "providers.authEnv.present" : "providers.authEnv.missing")}</p>;
}

function Models({ models }: { models: ProviderModel[] }): ReactElement | null {
  const t = useT();
  if (!models.length) return null;
  return <div className="control-table-wrap"><table className="control-table" data-testid="provider-models">
    <thead><tr><th>{t("providers.model")}</th><th>{t("providers.digest")}</th><th>{t("providers.loaded")}</th><th>{t("providers.level.governance")}</th></tr></thead>
    <tbody>{models.map((model) => <tr key={model.model} data-testid={`model-${model.model}`}>
      <td><strong>{model.model}</strong>{model.governance !== "AUTHORIZED_AND_MEASURED" && <small>{t("providers.nonAuthoritative")}</small>}</td>
      <td><code>{shortDigest(model.digest)}</code></td>
      <td>{t(model.loaded === true ? "providers.loaded.yes" : model.loaded === false ? "providers.loaded.no" : "providers.loaded.unknown")}</td>
      <td><Tone tone={governanceTone(model.governance)} text={t(governanceKey(model.governance))} testId="model-governance" />
        <small>{t(`providers.govHelp.${model.governance}`)}</small>
        {model.profile_id && <small>{t("providers.profile")}: <code>{model.profile_id.slice(0, 16)}…</code>{model.measured_runtime_version ? ` · ${model.measured_runtime_version}` : ""}</small>}
        {model.reasons.length > 0 && <details><summary>{t("providers.reasons")}</summary>{model.reasons.map((reason) => <div key={reason}><Reason code={reason} /></div>)}</details>}</td>
    </tr>)}</tbody></table></div>;
}

function Checks({ checks }: { checks: ProviderCheck[] }): ReactElement | null {
  const t = useT();
  if (!checks.length) return null;
  const label = (name: string) => {
    const key = `providers.check.${name}` as const;
    return ["contract", "health", "discovery", "model_identity", "governed_binding", "probe_chat"].includes(name) ? t(key as "providers.check.health") : name;
  };
  return <details className="provider-checks" open><summary>{t("providers.checks")}</summary>
    {checks.map((check) => <div className="control-row" key={check.check} data-testid={`check-${check.check}`}>
      <span>{label(check.check)}</span>
      <Tone tone={check.status === "PASS" || check.status === "BOUND" ? "ok" : check.status === "SKIPPED" ? "unknown" : "error"} text={check.status} />
      <Reason code={check.code} />
      {check.check === "probe_chat" && check.status === "PASS" && <small>{t("providers.nonAuthoritative")} · {check.answer_chars} chars · <code>{(check.answer_sha256 || "").slice(0, 12)}…</code></small>}
      {check.reasons && check.reasons.length > 0 && <details><summary>{t("providers.reasons")}</summary>{check.reasons.map((reason) => <div key={reason}><Reason code={reason} /></div>)}</details>}
    </div>)}
  </details>;
}

function EditForm({ provider, onSubmit, busy }: { provider: Provider; onSubmit: (update: ProviderUpdate) => void; busy: boolean }): ReactElement {
  const t = useT();
  const [form, setForm] = useState<ProviderForm>(() => formFrom(provider.settings));
  const [problem, setProblem] = useState<MessageKey | null>(null);
  const gateway = provider.kind === "enterprise_gateway";
  const field = (name: keyof ProviderForm) => ({ name, value: form[name], onChange: (event: { target: { value: string } }) => setForm({ ...form, [name]: event.target.value }) });
  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const result = updateFrom(provider.kind, form);
    if ("error" in result) {
      setProblem(result.error);
      return;
    }
    setProblem(null);
    onSubmit(result.update);
  }
  return <form className="provider-form" onSubmit={submit} aria-label={t("providers.edit")}>
    <p className="control-muted">{t("providers.form.help")}</p>
    <label>{t("providers.form.displayName")}<input className="control-input" {...field("display_name")} /></label>
    {gateway ? <label>{t("providers.form.contractPath")}<input className="control-input" {...field("contract_path")} /></label>
      : <label>{t("providers.form.endpoint")}<input className="control-input" {...field("endpoint")} /></label>}
    <label>{t("providers.form.timeout")}<input className="control-input" inputMode="numeric" {...field("timeout_seconds")} /></label>
    <label>{t("providers.form.context")}<input className="control-input" inputMode="numeric" {...field("context_window")} /></label>
    <label>{t("providers.form.requirement")}<select className="control-input" {...field("capability_requirement")}>
      <option value="">{t("providers.form.requirement.none")}</option>{REQUIREMENTS.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
    <label>{t("providers.form.projects")}<input className="control-input" {...field("project_ids")} /></label>
    <label>{t("providers.form.classification")}<select className="control-input" {...field("max_classification")}>
      {CLASSIFICATIONS.filter((item) => !gateway || item !== "RESTRICTED").map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
    <label>{t("providers.form.concurrency")}<input className="control-input" inputMode="numeric" {...field("max_concurrent_calls")} /></label>
    <label>{t("providers.form.maxOutput")}<input className="control-input" inputMode="numeric" {...field("max_output_tokens")} /></label>
    {!gateway && <label>{t("providers.form.authEnv")}<input className="control-input" autoComplete="off" {...field("auth_env")} /></label>}
    {problem && <p className="control-error" role="alert" data-testid="form-problem">{t(problem)}</p>}
    <button className="control-button" type="submit" disabled={busy}>{t("providers.save")}</button>
  </form>;
}

function CatalogAndAuthorizations({ overview }: { overview: ProvidersOverview }): ReactElement {
  const t = useT();
  return <div className="control-grid">
    <Section title={t("providers.catalog")} testId="catalog-profiles">
      {overview.catalog.profiles.length === 0 ? <p className="product-empty">{t("providers.catalog.empty")}</p>
        : overview.catalog.profiles.map((profile) => <div className="control-row" key={profile.profile_id}>
          <strong>{profile.artifact_ref}</strong><code>{shortDigest(profile.manifest_digest)}</code><small>{profile.runtime_family} {profile.runtime_version}</small></div>)}
    </Section>
    <Section title={t("providers.authorizations")} testId="authorizations">
      {overview.authorizations.registrations.length === 0 ? <p className="product-empty">{t("providers.authorizations.empty")}</p>
        : overview.authorizations.registrations.map((item) => <div className="control-row" key={item.node_id}>
          <strong>{item.node_name || item.node_id}</strong><span>{t(item.authorized ? "providers.registration.authorized" : "providers.registration.notAuthorized")}</span>
          <small>{item.max_classification} · {item.allowed_models.length} models · {item.project_ids.join(", ") || "—"}</small></div>)}
    </Section>
  </div>;
}
