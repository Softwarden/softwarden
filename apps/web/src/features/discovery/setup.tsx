/** Discovery › Set up a source: choose a source, then a guided walkthrough per source in five steps,
 *  Prepare, Connect, Scope, Check and Finish, so the setup can be done from the on-screen instructions and the
 *  permissions reference (docs/PERMISSIONS.md) alone. The step, the connector and the run are in the URL
 *  (`?step=scope&connector=…&run=…`), so a reload resumes where it was. A source shows as unavailable without its
 *  capability (every edition has every source, so only when it was turned off for the organization). Admin only (other roles see who may open it). Loaded on first use. */
import { uuidOrEmpty } from "@/lib/safe-path";
import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, apiWithEtag, PRECONDITION_FAILED, Problem, type Connection, type ConnectionKind, type Connector, type ConnectorRun, type DiscoverySourceInfo, type ProbeResult } from "@/api";
import { AdminOnly, Breadcrumb, EmptyState, ErrorBanner, ErrorSummary, Field, Loading, PageHead, Select } from "@/components/app";
import { chip, DetailList, DetailRow } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { ifMatch, isStaleWrite } from "@/lib/if-match";
import { useMe } from "@/lib/session";
import { ConnectionDialog, connectionsPath, useConnectionTest } from "@/features/discovery/connections";
import {
  AccessCheckResult, CONNECTION_LABEL, CONNECTION_TEXT, ConnectionLine, docLink, PermissionsTable, ProbeResultNote, RunResult, RunStatusChip, runSentence,
  PREVIEW, sourceInfo, sourcesFor, stepsFor, testState, TestStateChip, type SourceInfo, unavailableReason,
} from "@/features/discovery/discovery-ui";
import { scopeBody, scopeErrors, ScopeFields, scopeValues, type ScopeValues } from "@/features/discovery/scope-form";

const WHAT = "the guided setup of discovery sources: connections with their secrets, scopes and access checks";

/** If-Match for a step's save: the connector's ETag read now, while the fields the step sends are still the ones
 *  it started from (a run changes the connector's revision without touching them). Otherwise someone else changed them:
 *  a 412, said as STALE_STEP. */
async function ifUnchanged(start: Connector, fields: (c: Connector) => unknown): Promise<Record<string, string> | undefined> {
  const now = await apiWithEtag<Connector>(`connectors/${start.id}`);
  if (JSON.stringify(fields(now.data)) !== JSON.stringify(fields(start))) throw new Problem(412, STALE_STEP, {}, {}, PRECONDITION_FAILED);
  return ifMatch(now.etag);
}
const STALE_STEP = "Someone else changed this connector after this step opened, so nothing was saved. Reload the page to see their changes, then make yours again.";

// ---- choose a source ------------------------------------------------------------------------------------------------

export function SourcePickerPage() {
  return <AdminOnly title="Set up a discovery source" what={WHAT}><SourcePicker /></AdminOnly>;
}

function Unavailable({ info }: { info: SourceInfo }) {
  const me = useMe();
  // A preview source opened by its address while the installation keeps preview features off.
  const hidden = info.preview && !me.capabilities.includes(PREVIEW);
  return (
    <p className="rounded-md border border-dashed border-border-strong bg-muted px-3 py-2 text-sm" data-unavailable="">
      <span aria-hidden="true">⊘ </span><span className="font-semibold">{info.label} isn't available.</span>{" "}
      {hidden ? "It is a preview feature this installation hasn't turned on." : unavailableReason()}
    </p>
  );
}

function SourcePicker() {
  const me = useMe();
  const connectors = useApiQuery<Connector[]>("connectors");
  return (
    <>
      <Breadcrumb trail={[{ to: "/discovery", label: "Discovery" }]} current="Set up a source" />
      <PageHead title="Set up a discovery source" desc="Each source is read-only: Softwarden lists what it finds as candidates and never changes anything in the source. The guided setup takes about fifteen minutes, most of it in the source's admin center." />
      {connectors.isError && <ErrorBanner message={problemText(connectors.error, "Couldn't load the connectors you already have.")} onRetry={() => void connectors.refetch()} />}
      <ul className="m-0 grid list-none grid-cols-1 gap-3 p-0 md:grid-cols-2" aria-label="Sources">
        {sourcesFor(me.capabilities).map((s) => {
          const available = me.capabilities.includes(s.capability);
          const existing = (connectors.data ?? []).filter((c) => c.source === s.id);
          return (
            <li key={s.id} className="flex flex-col gap-2 rounded-lg border bg-card px-4 py-3" data-source={s.id}>
              <h2 className="font-display text-section font-semibold">{s.label}</h2>
              <p className="text-sm text-muted-foreground">{s.summary}</p>
              {existing.length > 0 && (
                <p className="text-sm">Already set up: {existing.map((c, i) => <span key={c.id}>{i > 0 && ", "}<Link to={`/discovery/connectors/${c.id}`} className="underline">{c.name}</Link></span>)}</p>
              )}
              <div className="mt-auto">
                {available
                  ? <Button asChild variant="outline" className="h-auto min-h-9 whitespace-normal text-left"><Link to={`/discovery/setup/${s.id}`}>Set up {s.label}</Link></Button>
                  : <Unavailable info={s} />}
              </div>
            </li>
          );
        })}
      </ul>
    </>
  );
}

// ---- the walkthrough ------------------------------------------------------------------------------------------------

const STEPS = [
  { id: "prepare", label: "Prepare" }, { id: "connect", label: "Connect" }, { id: "scope", label: "Scope" },
  { id: "check", label: "Check access" }, { id: "finish", label: "Finish" },
] as const;
type StepId = (typeof STEPS)[number]["id"];

export function WalkthroughPage() {
  return <AdminOnly title="Set up a discovery source" what={WHAT}><Walkthrough /></AdminOnly>;
}

function Walkthrough() {
  const { source = "" } = useParams();
  const me = useMe();
  const info = sourceInfo(source);
  if (!info) {
    return (
      <>
        <PageHead title="Source not found" desc={`Softwarden has no discovery source called “${source}”. The link may be incomplete.`} />
        <Button asChild variant="outline"><Link to="/discovery/setup">Back to Set up a source</Link></Button>
      </>
    );
  }
  // A preview source whose capability the organization has anyway (license usage is under discovery.entra) is
  // still closed while the installation keeps preview features off.
  if (!me.capabilities.includes(info.capability) || (info.preview && !me.capabilities.includes(PREVIEW))) {
    return (
      <>
        <Breadcrumb trail={[{ to: "/discovery", label: "Discovery" }, { to: "/discovery/setup", label: "Set up a source" }]} current={info.label} />
        <PageHead title={`Set up ${info.label}`} />
        <Unavailable info={info} />
        <div className="mt-3"><Button asChild variant="outline"><Link to="/discovery/setup">Choose another source</Link></Button></div>
      </>
    );
  }
  return <Steps info={info} />;
}

function StepHeading({ children }: { children: React.ReactNode }) {
  return <h2 id="step-heading" tabIndex={-1} className="mb-3 font-display text-dialog font-semibold outline-none">{children}</h2>;
}

function Steps({ info }: { info: SourceInfo }) {
  const [params, setParams] = useSearchParams();
  const connectorId = uuidOrEmpty(params.get("connector"));
  const asked = (params.get("step") ?? "prepare") as StepId;
  // A step after Connect needs the connector; without one the walkthrough resumes at Connect.
  const step: StepId = !STEPS.some((s) => s.id === asked) ? "prepare" : !connectorId && !["prepare", "connect"].includes(asked) ? "connect" : asked;
  const at = STEPS.findIndex((s) => s.id === step);
  const connector = useApiQuery<Connector>(connectorId ? `connectors/${connectorId}` : "connectors/none", { enabled: !!connectorId });
  const go = (to: StepId, extra: Record<string, string> = {}) => {
    const next = new URLSearchParams(params);
    next.set("step", to);
    for (const [k, v] of Object.entries(extra)) { if (v) next.set(k, v); else next.delete(k); }
    if (to !== "finish") next.delete("run");
    setParams(next);
  };
  // Focus the step's heading when the step changes (not on the first render: the page heading leads then).
  const first = useRef(true);
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    requestAnimationFrame(() => document.getElementById("step-heading")?.focus());
  }, [step]);
  const c = connectorId ? connector.data : null;
  const wrongSource = c && c.source !== info.id;
  return (
    <>
      <Breadcrumb trail={[{ to: "/discovery", label: "Discovery" }, { to: "/discovery/setup", label: "Set up a source" }]} current={info.label} />
      <PageHead title={`Set up ${info.label}`} desc={info.summary} />
      <nav aria-label="Setup progress" className="mb-5">
        <ol className="flex flex-wrap gap-x-5 gap-y-2">
          {STEPS.map((s, i) => {
            const done = i < at;
            const reachable = done && (i < 2 || !!connectorId);
            const mark = (
              <span aria-hidden="true" className={`grid h-6 w-6 place-items-center rounded-full border text-xs font-semibold ${i === at ? "border-accent-line bg-primary text-primary-foreground" : done ? "border-success-border bg-success-bg text-success-foreground" : "border-border-strong"}`}>
                {done ? "✓" : i + 1}
              </span>
            );
            return (
              <li key={s.id} aria-current={i === at ? "step" : undefined} className={`flex items-center gap-2 text-sm ${i === at ? "font-semibold" : "text-muted-foreground"}`}>
                {mark}
                {reachable
                  ? <Link to={`?${new URLSearchParams({ ...Object.fromEntries(params), step: s.id }).toString()}`} className="inline-block min-h-6 leading-6">{s.label}<span className="sr-only"> (done, go back to it)</span></Link>
                  : <span>{s.label}{done && <span className="sr-only"> (done)</span>}</span>}
              </li>
            );
          })}
        </ol>
      </nav>
      {connectorId && connector.isError && <ErrorBanner message={problemText(connector.error, "Couldn't load the connector.")} onRetry={() => void connector.refetch()} />}
      {connectorId && !c && !connector.isError && <Loading what="the connector" />}
      {wrongSource && <ErrorBanner message={`${c.name} reads ${c.source_label}, not ${info.label}. Open its own settings instead.`} />}
      {(!connectorId || (c && !wrongSource)) && (
        <section aria-labelledby="step-heading" className="max-w-3xl">
          {step === "prepare" && <PrepareStep info={info} connector={c ?? null} onNext={() => go("connect")} />}
          {step === "connect" && <ConnectStep info={info} connector={c ?? null} onNext={(id) => go("scope", { connector: id })} />}
          {step === "scope" && c && <ScopeStep connector={c} onNext={() => go("check")} />}
          {step === "check" && c && <CheckStep info={info} connector={c} onNext={() => go("finish")} onBack={() => go("connect")} />}
          {step === "finish" && c && <FinishStep connector={c} runId={uuidOrEmpty(params.get("run"))} onRun={(id) => go("finish", { run: id })} onCheck={() => go("check")} />}
        </section>
      )}
    </>
  );
}

// ---- 1. prepare -----------------------------------------------------------------------------------------------------

function PrepareStep({ info, connector, onNext }: { info: SourceInfo; connector: Connector | null; onNext: () => void }) {
  // The permissions are the source's: this connector's, or before one exists, the source's from discovery/sources.
  const sources = useApiQuery<DiscoverySourceInfo[]>("discovery/sources", { enabled: !connector });
  const permissions = connector?.permissions ?? sources.data?.find((s) => s.id === info.id)?.permissions ?? null;
  const me = useMe();
  return (
    <>
      <StepHeading>Prepare</StepHeading>
      <p className="mb-3 max-w-prose">
        You need someone who can manage {PREPARE_WHO[info.connection]}. Softwarden never grants a permission itself: it reads only, with what you grant here.
      </p>
      <ol className="mb-4 ml-5 flex max-w-prose list-decimal flex-col gap-2">
        {stepsFor(info, !!me.simulation_allowed).map((s, i) => <li key={i}>{s}</li>)}
      </ol>
      <p className="mb-4 max-w-prose text-sm">
        Every permission, why it is needed and how it was checked: <Link className="underline" to={docLink(info.doc)}>permissions reference, {info.label}</Link>.
      </p>
      <section aria-labelledby="permissions-h" className="mb-5">
        <h3 id="permissions-h" className="mb-1.5 font-semibold">Permissions this source uses</h3>
        {permissions && permissions.length > 0
          ? <PermissionsTable permissions={permissions} caption={`Permissions ${info.label} uses`} />
          : sources.isLoading && !connector
            ? <Loading what="the permissions" />
            : sources.isError
              ? <ErrorBanner message={problemText(sources.error, "Couldn't load the permissions this source uses. The steps above and the permissions reference name them.")} onRetry={() => void sources.refetch()} />
              : <p className="max-w-prose text-sm text-muted-foreground">This source asks for no permission of its own. The steps above say what to set up.</p>}
      </section>
      <div className="flex flex-wrap gap-2 border-t pt-4">
        <Button onClick={onNext}>Continue to Connect</Button>
        <Button asChild variant="outline"><Link to="/discovery/setup">Choose another source</Link></Button>
      </div>
    </>
  );
}

/** Who has to prepare the source, by its connection kind. */
const PREPARE_WHO: Record<ConnectionKind, string> = {
  microsoft: "app registrations and grant admin consent in your Microsoft tenant",
  halo: "API applications in HaloITSM",
  okta: "API service apps and admin roles in your Okta org",
  google_workspace: "a Google Cloud project and, for domain-wide delegation, a super administrator of your Google Workspace",
};

// ---- 2. connect -----------------------------------------------------------------------------------------------------

function ConnectStep({ info, connector, onNext }: { info: SourceInfo; connector: Connector | null; onNext: (id: string) => void }) {
  const kind: ConnectionKind = info.connection;
  const list = useApiQuery<Connection[]>(connectionsPath(kind));
  const all = useApiQuery<Connector[]>("connectors", { enabled: !connector });
  const invalidate = useInvalidate();
  const tests = useConnectionTest(kind, () => invalidate(connectionsPath(kind)));
  // The connector as this step started: its save compares against these, not the live prop, which refetches.
  const [start] = useState(connector);
  const [chosen, setChosen] = useState(connector?.connection?.id ?? "");
  const [name, setName] = useState(connector?.name ?? "");
  const [dialog, setDialog] = useState<Connection | "new" | null>(null);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [saving, setSaving] = useState(false);
  const rows = list.data;
  // Start with the only connection there is, and a name no other connector has.
  useEffect(() => { if (!chosen && rows?.length === 1) setChosen(rows[0].id); }, [rows, chosen]);
  useEffect(() => {
    if (connector || name || !all.data) return;
    const taken = new Set(all.data.map((c) => c.name.toLowerCase()));
    let candidate = info.label;
    for (let k = 2; taken.has(candidate.toLowerCase()); k++) candidate = `${info.label} (${k})`;
    setName(candidate);
  }, [all.data, connector, name, info.label]);
  const picked = rows?.find((r) => r.id === chosen) ?? null;
  const attr = `${kind}_connection`;
  async function next() {
    const problems: Record<string, string[]> = {};
    if (!name.trim()) problems.connector_name = ["Enter a name for the connector."];
    if (!picked) problems[attr] = [rows?.length ? "Choose a connection, or add one." : "Add a connection first."];
    else if (!picked.test_current) problems[attr] = [`Test ${picked.name} first: a connector runs only on a connection that passed its test since its last change.`];
    if (Object.keys(problems).length) return setErrors(problems);
    setSaving(true);
    setErrors({});
    try {
      const body = { name: name.trim(), [attr]: picked!.id };
      const saved = connector
        ? await api<Connector>(`connectors/${connector.id}`, { method: "PATCH", body, headers: await ifUnchanged(start ?? connector, (x) => [x.name, x.connection?.id ?? null]) })
        : await api<Connector>("connectors", { method: "POST", body: { source: info.id, ...body } });
      void invalidate("connectors", connectionsPath(kind));
      onNext(saved.id);
    } catch (e) {
      if (isStaleWrite(e)) setErrors({ connector_name: [STALE_STEP] });
      else if (e instanceof Problem && Object.keys(e.errors).length) setErrors(Object.fromEntries(Object.entries(e.errors).map(([k, v]) => [k === "name" ? "connector_name" : k, v])));
      else setErrors({ [attr]: [problemText(e, "Couldn't save the connector.")] });
    } finally {
      setSaving(false);
    }
  }
  return (
    <>
      <StepHeading>Connect</StepHeading>
      <p className="mb-3 max-w-prose">
        Choose the {CONNECTION_LABEL[kind]} this connector signs in with, or add one with the values from Prepare, then test it. A test proves the sign-in only; the access check in step 4 proves the permissions.
      </p>
      <ErrorSummary errors={errors} focusOnShow="field" />
      <Field id="connector_name" label="Connector name (required)" errors={errors} help="Shown on the Discovery page and in run history.">
        <Input id="connector_name" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
      </Field>
      {list.isError && <ErrorBanner message={problemText(list.error, "Couldn't load the connections.")} onRetry={() => void list.refetch()} />}
      {!rows && !list.isError && <Loading what="connections" />}
      {rows && (
        <fieldset className="mb-3" aria-describedby={errors[attr] ? `${attr}-error` : undefined}>
          <legend className="mb-1.5 font-semibold">Connection</legend>
          {rows.length === 0 && (
            <EmptyState title={`No ${CONNECTION_LABEL[kind]} yet`}>
              {CONNECTION_TEXT[kind].empty}
            </EmptyState>
          )}
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {rows.map((r, i) => {
              const state = testState(r);
              return (
                <li key={r.id} className={`rounded-md border bg-card px-3 py-2 ${r.id === chosen ? "border-accent-line" : ""}`}>
                  <label className="flex min-h-8 flex-wrap items-center gap-2">
                    <input type="radio" name="connection" id={i === 0 ? attr : `${attr}-${r.id}`} value={r.id} checked={r.id === chosen} onChange={() => setChosen(r.id)} className="h-4 w-4 accent-primary" />
                    <span className="font-semibold">{r.name}</span>
                    <TestStateChip c={r} />
                    <span className="text-sm text-muted-foreground">{kind === "microsoft" ? `tenant ${r.tenant_id}` : kind === "google_workspace" ? `as ${r.admin_email}` : r.base_url}</span>
                  </label>
                  {r.id === chosen && (
                    <div className="mt-1 ml-6 flex flex-wrap items-center gap-2">
                      {state.note && <span className="text-sm">{state.note}</span>}
                      <Button type="button" size="sm" variant="outline" disabled={tests.testing === r.id} onClick={() => void tests.test(r)}>{tests.testing === r.id ? "Testing…" : "Test connection"}<span className="sr-only"> {r.name}</span></Button>
                      <Button type="button" size="sm" variant="outline" onClick={() => setDialog(r)}>Edit<span className="sr-only"> {r.name}</span></Button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
          {errors[attr] && <p id={`${attr}-error`} className="mt-1 text-sm text-error-foreground">{errors[attr].join(" ")}</p>}
          {/* With no connection to choose, the error's link (and focus) goes to this button. */}
          <Button type="button" id={rows.length === 0 ? attr : undefined} variant="outline" className="mt-2" onClick={() => setDialog("new")}>Add a {CONNECTION_LABEL[kind]}</Button>
        </fieldset>
      )}
      <div className="mb-4" role="status" data-testid="connection-test-result">
        {tests.testing && <p className="text-muted-foreground">Asking {CONNECTION_TEXT[kind].provider} for a token…</p>}
        {tests.outcome && <ProbeResultNote name={tests.outcome.name} result={tests.outcome.result as ProbeResult} />}
        {tests.error && <p className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2"><span aria-hidden="true">! </span>{tests.error}</p>}
      </div>
      <div className="flex flex-wrap gap-2 border-t pt-4">
        <Button onClick={() => void next()} disabled={saving}>{saving ? "Saving…" : "Save and continue to Scope"}</Button>
      </div>
      <ConnectionDialog kind={kind} item={dialog} onClose={() => setDialog(null)}
        onDone={(_text, created) => {
          setDialog(null);
          void invalidate(connectionsPath(kind));
          const target = created ?? (dialog !== "new" ? dialog : null);
          if (target) { setChosen(target.id); void tests.test(target); }
        }} />
    </>
  );
}

// ---- 3. scope -------------------------------------------------------------------------------------------------------

function ScopeStep({ connector, onNext }: { connector: Connector; onNext: () => void }) {
  // The connector as this step started: its save compares against these, not the live prop, which refetches.
  const [start] = useState(connector);
  const [values, setValues] = useState<ScopeValues>(() => scopeValues(connector.source, connector.scope));
  const [schedule, setSchedule] = useState(connector.schedule);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [saving, setSaving] = useState(false);
  const invalidate = useInvalidate();
  async function next(e: React.FormEvent) {
    e.preventDefault();
    if (saving) return;
    setSaving(true);
    setErrors({});
    try {
      await api<Connector>(`connectors/${connector.id}`, {
        method: "PATCH", body: { scope: scopeBody(connector.source, values), schedule }, headers: await ifUnchanged(start, (x) => [x.scope, x.schedule]),
      });
      await invalidate(`connectors/${connector.id}`);
      onNext();
    } catch (err) {
      if (isStaleWrite(err)) setErrors({ schedule: [STALE_STEP] });
      else setErrors(err instanceof Problem && Object.keys(err.errors).length ? scopeErrors(connector.source, err.errors) : { schedule: [problemText(err, "Couldn't save the scope.")] });
    } finally {
      setSaving(false);
    }
  }
  return (
    <form onSubmit={next} noValidate>
      <StepHeading>Scope</StepHeading>
      <p className="mb-3 max-w-prose">What this connector reads. Anything the scope leaves out is counted in each run under the filter that skipped it, never stored. You can change the scope later in the connector's settings.</p>
      <ErrorSummary errors={errors} focusOnShow="field" />
      <ScopeFields source={connector.source} values={values} onChange={setValues} errors={errors} />
      <Field id="schedule" label="Run" errors={errors}>
        <Select id="schedule" value={schedule} onChange={(e) => setSchedule(e.target.value as Connector["schedule"])} className="w-80 max-w-full">
          <option value="manual">Only when someone runs it</option><option value="daily">Daily, after 02:00 organization time</option>
        </Select>
      </Field>
      <div className="flex flex-wrap gap-2 border-t pt-4">
        <Button type="submit" disabled={saving}>{saving ? "Saving…" : "Save and continue to Check access"}</Button>
      </div>
    </form>
  );
}

// ---- 4. check -------------------------------------------------------------------------------------------------------

function CheckStep({ info, connector, onNext, onBack }: { info: SourceInfo; connector: Connector; onNext: () => void; onBack: () => void }) {
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const [blocked, setBlocked] = useState("");
  const invalidate = useInvalidate();
  const check = connector.access_check;
  async function run() {
    setChecking(true);
    setError("");
    setBlocked("");
    try {
      await api<Connector>(`connectors/${connector.id}/access-check`, { method: "POST" });
      await invalidate(`connectors/${connector.id}`, "connectors");
    } catch (e) {
      setError(problemText(e, "Couldn't run the access check."));
    } finally {
      setChecking(false);
    }
  }
  function next() {
    if (!check) return setBlocked("Check access first.");
    if (!check.ok) return setBlocked(check.missing.length ? `Grant ${check.missing.join(", ")} (see the failed step), then check again.` : "Fix the failed step, then check again.");
    onNext();
  }
  return (
    <>
      <StepHeading>Check access</StepHeading>
      <p className="mb-3 max-w-prose">
        Softwarden signs in with the connection and makes one one-item read per permission the source uses, so it can say exactly which permission is missing. Nothing is changed in the source. A check takes at most three minutes.
      </p>
      <div className="mb-3 flex flex-wrap gap-2">
        <Button variant={check?.ok ? "outline" : "default"} onClick={() => void run()} disabled={checking}>{checking ? "Checking…" : check ? "Check access again" : "Check access"}</Button>
        <Button asChild variant="outline"><Link to={docLink(info.doc)}>What to grant</Link></Button>
      </div>
      <div role="status" className="mb-3">
        {checking && <p className="text-muted-foreground">Checking access…</p>}
        {error && <p className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2"><span aria-hidden="true">! </span>{error}</p>}
      </div>
      {check ? <AccessCheckResult check={check} /> : !checking && <p className="mb-3 text-muted-foreground">Not checked yet.</p>}
      {check && !check.ok && check.steps.some((s) => s.group === "credentials" && s.state === "failed") && (
        <p className="mb-3 text-sm">The sign-in failed: fix the connection in <Button variant="link" className="h-auto p-0 align-baseline underline" onClick={onBack}>Connect</Button>, then check again.</p>
      )}
      {blocked && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert"><span aria-hidden="true">! </span>{blocked}</p>}
      <div className="flex flex-wrap gap-2 border-t pt-4">
        <Button variant={check?.ok ? "default" : "outline"} onClick={next}>Continue to Finish</Button>
      </div>
    </>
  );
}

// ---- 5. finish ------------------------------------------------------------------------------------------------------

const ACTIVE = (r: ConnectorRun | null | undefined) => !!r && (r.status === "queued" || r.status === "running");

function FinishStep({ connector, runId, onRun, onCheck }: { connector: Connector; runId: string; onRun: (id: string) => void; onCheck: () => void }) {
  const invalidate = useInvalidate();
  // Resume: a run in the URL, else the connector's queued or running run.
  const id = runId || connector.active_run?.id || "";
  const run = useApiQuery<ConnectorRun>(id ? `connectors/${connector.id}/runs/${id}` : "connectors/none", {
    enabled: !!id,
    // Live progress: ask again every two seconds while the run is queued or running.
    refetchInterval: (q) => (q.state.status === "error" ? false : ACTIVE(q.state.data as ConnectorRun | undefined) || !q.state.data ? 2000 : false),
  });
  const [starting, setStarting] = useState(false);
  const [refused, setRefused] = useState("");
  const r = id ? run.data : null;
  const finished = r && !ACTIVE(r);
  useEffect(() => { if (finished) void invalidate("connectors"); }, [finished]); // eslint-disable-line react-hooks/exhaustive-deps
  async function start() {
    setStarting(true);
    setRefused("");
    try {
      const c = await api<Connector>(`connectors/${connector.id}/run`, { method: "POST" });
      if (c.active_run) onRun(c.active_run.id);
      void invalidate("connectors");
    } catch (e) {
      setRefused(problemText(e, "Couldn't start the run."));
    } finally {
      setStarting(false);
    }
  }
  const live = !r ? "" : r.status === "queued" ? "Run queued. The worker starts it within a minute." : r.status === "running" ? "Running." : `Run ${r.status === "succeeded" ? "finished" : "failed"}. ${runSentence(r)}`;
  return (
    <>
      <StepHeading>Finish</StepHeading>
      <DetailList className="mb-4 rounded-lg border bg-card">
        <DetailRow label="Connector">{connector.name} <span className="text-muted-foreground">({connector.source_label})</span></DetailRow>
        <DetailRow label="Connection"><ConnectionLine c={connector.connection} admin /></DetailRow>
        <DetailRow label="Access check">
          {connector.access_check
            ? connector.access_check.ok ? <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>Every required permission works</span> : <span className={chip({ state: "overdue" })}><span aria-hidden="true">!</span>Missing {connector.access_check.missing.join(", ") || "a permission"}</span>
            : <span className={chip({ state: "unreviewed" })}><span aria-hidden="true">○</span>Not checked</span>}
        </DetailRow>
        <DetailRow label="Runs">{connector.schedule === "daily" ? "Daily, after 02:00 organization time, and when someone runs it" : "Only when someone runs it"}</DetailRow>
      </DetailList>
      <p className="mb-3 max-w-prose">Run it now to list what the source has. Each application it finds becomes a candidate to link to a record, create a record from, or dismiss; nothing changes in your records on its own.</p>
      {/* Reached by its address without a passing check (or the check was cleared by a change of connection or
          scope): check first, so a run never starts on permissions nobody verified. */}
      {!r && !connector.access_check?.ok && (
        <div className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-state-pending-foreground" data-testid="check-first">
          <p className="font-semibold"><span aria-hidden="true">◐ </span>Check access first.</p>
          <p>{connector.access_check ? `The last check found ${connector.access_check.missing.join(", ") || "a failed step"}.` : "This connector's access hasn't been checked since its connection or scope was set."} Run it once every required permission works.</p>
          <Button className="mt-2" onClick={onCheck}>Go to Check access</Button>
        </div>
      )}
      {!r && connector.access_check?.ok && (
        <div className="mb-3 flex flex-wrap gap-2">
          <Button onClick={() => void start()} disabled={starting}>{starting ? "Queueing…" : "Run now"}</Button>
        </div>
      )}
      {refused && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert"><span aria-hidden="true">! </span>Not started: {refused}</p>}
      <p className="sr-only" aria-live="polite">{live}</p>
      {id && run.isError && <ErrorBanner message={problemText(run.error, "Couldn't load the run.")} onRetry={() => void run.refetch()} />}
      {id && !r && !run.isError && <Loading what="the run" />}
      {r && (
        <section aria-labelledby="run-h" className="mb-4 rounded-lg border bg-card px-4 py-3" data-testid="run-progress">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <h3 id="run-h" className="font-semibold">This run</h3>
            <RunStatusChip status={r.status} />
          </div>
          {ACTIVE(r) && (
            <p className="mb-2">
              {r.status === "queued" ? "Waiting for the worker; it starts the run within a minute." : `Reading ${connector.source_label} since ${r.started_at ? new Date(r.started_at).toLocaleTimeString() : "a moment ago"}.`}{" "}
              This page updates on its own; you can leave it and come back.
            </p>
          )}
          {finished && <RunResult run={r} />}
        </section>
      )}
      {finished && (
        <div className="flex flex-wrap gap-2 border-t pt-4">
          {r.status === "succeeded"
            ? <Button asChild><Link to={`/discovery?connector=${connector.id}`}>Review the candidates</Link></Button>
            : <Button onClick={() => void start()} disabled={starting}>{starting ? "Queueing…" : "Run again"}</Button>}
          <Button asChild variant="outline"><Link to={`/discovery/connectors/${connector.id}`}>Connector settings</Link></Button>
          <Button asChild variant="outline"><Link to={`/discovery/runs/${r.id}`}>Run details</Link></Button>
        </div>
      )}
    </>
  );
}
