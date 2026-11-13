/** Discovery › Connections: the Microsoft and Halo credentials discovery connectors sign in with.
 *  Create, edit (If-Match), delete (refused while connectors use it, naming them) and test, with the outcome in plain
 *  words and what to do next. A connection whose sign-in values or secret changed says so until it is tested again:
 *  no connector runs on it until then. The client secret is write-only: "set on DATE" and a field to replace it. Halo
 *  needs the capability `discovery.halo`, Okta `discovery.okta` and Google Workspace `discovery.google`. Admin only (the API's reads are too). Loaded on first use. */
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, apiWithEtag, PRECONDITION_FAILED, Problem, when, type Connection, type ConnectionKind, type ProbeResult } from "@/api";
import { AdminOnly, EmptyState, ErrorBanner, ErrorSummary, Field, Loading, PageHead, TabStrip, TableRegion, Textarea, useReturnFocus } from "@/components/app";
import { SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useQueryClient } from "@tanstack/react-query";
import { apiKey, problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { ifMatch, isStaleWrite, staleDeleteText, useOpenedRecord } from "@/lib/if-match";
import { useMe } from "@/lib/session";
import { andList } from "@/lib/utils";
import { SecretState } from "@/features/notify/notify-ui";
import { useResetOn } from "@/features/notify/notify";
import { aboutFor, CONNECTION_LABEL, CONNECTION_TEXT, connectionKindsFor, docLink, ProbeResultNote, testState, TestStateChip, unavailableReason } from "@/features/discovery/discovery-ui";
import { destinationMoved } from "@/lib/destination";

export const connectionsPath = (kind: ConnectionKind) => `connections/${kind}`;
/** The capability a connection kind needs (Microsoft needs none). */
const KIND_CAPABILITY: Record<ConnectionKind, string | null> = {
  microsoft: null, halo: "discovery.halo", okta: "discovery.okta", google_workspace: "discovery.google",
};
export type TestOutcome = { name: string; result: ProbeResult };

export function ConnectionsPage() {
  // Named from what this installation offers: Okta and Google Workspace only with preview features on.
  const kinds = connectionKindsFor(useMe().capabilities);
  return (
    <AdminOnly title="Connections" what={`the ${andList(kinds.map((k) => CONNECTION_TEXT[k].provider))} credentials discovery connectors sign in with`}>
      <Connections />
    </AdminOnly>
  );
}

function Connections() {
  const me = useMe();
  const [params, setParams] = useSearchParams();
  // Okta and Google Workspace are preview features: no tab unless the installation turns them on.
  const kinds = connectionKindsFor(me.capabilities);
  const tabs = kinds.map((id) => ({ id, label: CONNECTION_TEXT[id].tab }));
  const tab: ConnectionKind = kinds.find((k) => k === params.get("tab")) ?? "microsoft";
  const capability = KIND_CAPABILITY[tab];
  const unavailable = !!capability && !me.capabilities.includes(capability);
  return (
    <>
      <PageHead title="Connections" desc="The credentials discovery connectors sign in with. Several connectors can share one connection. Secrets are stored encrypted and never shown again; replace one by typing a new value." />
      <p className="mb-4 max-w-prose text-sm text-muted-foreground">
        Test a connection after creating or changing it: a connector runs only on a connection that passed its test since its last change. A test proves the sign-in only; each connector's access check proves its permissions. What to grant is in the <Link className="underline" to={docLink("connection-test-built")}>permissions reference</Link>.
      </p>
      <TabStrip tabs={tabs} tab={tab} label="Connection kinds" onSelect={(t) => setParams(t === "microsoft" ? {} : { tab: t })} />
      <div role="tabpanel" id="tabpanel" aria-labelledby={`tab-${tab}`}>
        {unavailable
          ? (
            <p className="rounded-md border border-dashed border-border-strong bg-muted px-3 py-2 text-sm" data-unavailable="">
              <span aria-hidden="true">⊘ </span><span className="font-semibold">{CONNECTION_TEXT[tab].heading} aren't available.</span> {unavailableReason(`${CONNECTION_TEXT[tab].tab} discovery`)}
            </p>
          )
          : <ConnectionList kind={tab} />}
      </div>
    </>
  );
}

/** Test a connection; the outcome (this call's `result`) is kept for the page to show. The test answers with the
 *  connection as now stored (its `test_current`), which goes into the cached list before the outcome is shown: anything
 *  that acts on the connection once the outcome is on screen (the walkthrough's Continue) sees it tested, however long
 *  the list's reload in `onTested` takes. On a slow CI runner Continue was refused ("Test … first") in the moment
 *  between the outcome and that reload. */
export function useConnectionTest(kind: ConnectionKind, onTested: () => unknown) {
  const client = useQueryClient();
  const [testing, setTesting] = useState("");
  const [outcome, setOutcome] = useState<(TestOutcome & { id: string }) | null>(null);
  const [error, setError] = useState("");
  async function test(c: { id: string; name: string }) {
    setTesting(c.id);
    setOutcome(null);
    setError("");
    try {
      const { result, ...tested } = await api<Connection & { result: ProbeResult }>(`${connectionsPath(kind)}/${c.id}/test`, { method: "POST" });
      // A connection just created may not be in the list yet (its reload still on its way): it is added.
      client.setQueryData<Connection[]>(apiKey(connectionsPath(kind)), (rows) =>
        rows && (rows.some((r) => r.id === tested.id) ? rows.map((r) => (r.id === tested.id ? tested : r)) : [...rows, tested]));
      setOutcome({ id: c.id, name: tested.name, result });
      void onTested();
    } catch (e) {
      setError(`Couldn't test ${c.name}: ${problemText(e, "something went wrong. Try again.")}`);
    } finally {
      setTesting("");
    }
  }
  return { test, testing, outcome, error, clear: () => { setOutcome(null); setError(""); } };
}

function ConnectionList({ kind }: { kind: ConnectionKind }) {
  const path = connectionsPath(kind);
  const q = useApiQuery<Connection[]>(path);
  const invalidate = useInvalidate();
  const refresh = () => invalidate(path, "connectors");
  const tests = useConnectionTest(kind, refresh);
  const [editing, setEditing] = useState<Connection | "new" | null>(null);
  const [deleting, setDeleting] = useState<Connection | null>(null);
  const [notice, setNotice] = useState("");
  const noun = CONNECTION_LABEL[kind];
  const simulationAllowed = !!useMe().simulation_allowed;
  const rows = q.data;
  const done = (text: string, created?: Connection) => {
    setEditing(null);
    setDeleting(null);
    setNotice(text);
    tests.clear();
    void refresh();
    if (created) void tests.test(created);
  };
  return (
    <section aria-labelledby={`${kind}-heading`}>
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id={`${kind}-heading`} className="font-display text-section font-semibold">{CONNECTION_TEXT[kind].heading}</h2>
          <p className="max-w-prose text-sm text-muted-foreground">{aboutFor(kind, simulationAllowed)}</p>
        </div>
        <Button onClick={() => setEditing("new")}>Add a {noun}</Button>
      </div>
      <SuccessNote text={notice} />
      {q.isError && <ErrorBanner message={problemText(q.error, "Couldn't load the connections.")} onRetry={() => void q.refetch()} />}
      {!rows && !q.isError && <Loading what="connections" rows={2} />}
      {rows && rows.length === 0 && (
        <EmptyState title={`No ${noun}s yet`} action={<Button variant="outline" asChild><Link to="/discovery/setup">Set up a discovery source</Link></Button>}>
          {CONNECTION_TEXT[kind].empty} The guided setup also creates one.
        </EmptyState>
      )}
      {rows && rows.length > 0 && (
        <TableRegion label={`${noun}s table`}>
          <Table>
            <TableCaption className="sr-only">{noun}s, by name</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Name</TableHead><TableHead scope="col">Signs in to</TableHead><TableHead scope="col">Secret</TableHead>
              <TableHead scope="col">Test</TableHead><TableHead scope="col">Used by</TableHead><TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((c) => {
                const state = testState(c);
                return (
                  <TableRow key={c.id} className="align-top" data-testid={`connection-${c.name}`}>
                    <TableHead scope="row" className="whitespace-normal font-semibold text-foreground">
                      {c.name}
                      {!c.enabled && <span className="block text-sm font-normal text-muted-foreground">Turned off</span>}
                      {c.simulated && <span className="block text-sm font-normal text-muted-foreground">Synthetic data</span>}
                    </TableHead>
                    <TableCell className="whitespace-normal text-sm">
                      {kind === "microsoft"
                        ? <>Tenant <span className="font-mono text-xs">{c.tenant_id}</span><span className="block">Client <span className="font-mono text-xs">{c.client_id || "—"}</span></span></>
                        : kind === "google_workspace"
                          ? <>As {c.admin_email}<span className="block">Service account <span className="font-mono text-xs">{c.service_account_email || "—"}</span>, client ID <span className="font-mono text-xs">{c.client_id || "—"}</span></span></>
                          : <>{c.base_url}<span className="block">Client <span className="font-mono text-xs">{c.client_id}</span>, scope {c.scope}</span></>}
                    </TableCell>
                    <TableCell><SecretState setOn={c.secret_set_on} what="Secret" /></TableCell>
                    <TableCell className="max-w-xs whitespace-normal">
                      <TestStateChip c={c} />
                      {state.note && <span className="block text-sm">{state.note}</span>}
                      {c.last_test && (
                        <span className="block text-sm text-muted-foreground">
                          Last test {when(c.last_test.at)}{c.last_test.by ? ` by ${c.last_test.by.name}` : ""}: {c.last_test.ok ? "passed" : `${c.last_test.problem_label || "failed"}`}.
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="whitespace-normal">
                      {c.connectors.length === 0 ? <span className="text-muted-foreground">No connectors</span> : (
                        <ul>{c.connectors.map((u) => <li key={u.id}><Link to={`/discovery/connectors/${u.id}`} className="inline-block min-h-6 leading-6">{u.name}</Link></li>)}</ul>
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      <span className="inline-flex flex-wrap justify-end gap-2">
                        <Button size="sm" variant="outline" disabled={tests.testing === c.id} onClick={() => void tests.test(c)}>{tests.testing === c.id ? "Testing…" : "Test"}<span className="sr-only"> {c.name}</span></Button>
                        <Button size="sm" variant="outline" onClick={() => setEditing(c)}>Edit<span className="sr-only"> {c.name}</span></Button>
                        <Button size="sm" variant="outline" onClick={() => setDeleting(c)}>Delete<span className="sr-only"> {c.name}</span></Button>
                      </span>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      <div className="mt-3" role="status" data-testid="connection-test-result">
        {tests.outcome && <ProbeResultNote name={tests.outcome.name} result={tests.outcome.result} />}
        {tests.error && <p className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2"><span aria-hidden="true">! </span>{tests.error}</p>}
      </div>
      <ConnectionDialog kind={kind} item={editing} onClose={() => setEditing(null)} onDone={done} />
      <DeleteConnectionDialog kind={kind} item={deleting} onClose={() => setDeleting(null)} onDone={(text) => done(text)} />
    </section>
  );
}

// ---- create and edit ------------------------------------------------------------------------------------------------

/** Create or edit a connection. An edit reads the connection with its ETag and saves with If-Match (412: someone
 *  changed it; load the latest). Only changed values are sent; the secret only when one is typed. `onDone` gets the
 *  created connection, which the caller tests. */
export function ConnectionDialog({ kind, item, onClose, onDone }: {
  kind: ConnectionKind; item: Connection | "new" | null; onClose: () => void; onDone: (text: string, created?: Connection) => void;
}) {
  const open = item !== null;
  const returnFocus = useReturnFocus(open);
  const existing = item && item !== "new" ? item : null;
  const noun = CONNECTION_LABEL[kind];
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>{existing ? `Edit ${existing.name}` : `Add a ${noun}`}</DialogTitle>
          <DialogDescription>
            {existing ? "Leave the secret empty to keep the stored one; it is never shown. A change to how it signs in needs a new test before connectors run." : "Saved and then tested at once. The secret is stored encrypted and never shown again."}
          </DialogDescription>
        </DialogHeader>
        {open && <ConnectionForm key={existing?.id ?? "new"} kind={kind} existing={existing} onCancel={onClose} onDone={onDone} />}
      </DialogContent>
    </Dialog>
  );
}

type Form = {
  name: string; tenant_id: string; client_id: string; base_url: string; scope: string; key_id: string; admin_email: string; secret: string;
  enabled: boolean; notes: string;
};
const FIELDS: Record<ConnectionKind, (keyof Form)[]> = {
  microsoft: ["name", "tenant_id", "client_id", "enabled", "notes"],
  halo: ["name", "base_url", "client_id", "scope", "enabled", "notes"],
  okta: ["name", "base_url", "client_id", "key_id", "scope", "enabled", "notes"],
  google_workspace: ["name", "admin_email", "enabled", "notes"],
};
const DEFAULT_NAME: Record<ConnectionKind, string> = { microsoft: "Microsoft Entra", halo: "HaloITSM", okta: "Okta", google_workspace: "Google Workspace" };

function ConnectionForm({ kind, existing, onCancel, onDone }: { kind: ConnectionKind; existing: Connection | null; onCancel: () => void; onDone: (text: string, created?: Connection) => void }) {
  const path = connectionsPath(kind);
  const startFrom = (c: Connection | null): Form => ({
    name: c?.name ?? DEFAULT_NAME[kind], tenant_id: c?.tenant_id ?? "", client_id: c?.client_id ?? "",
    base_url: c?.base_url ?? "", scope: c?.scope ?? "", key_id: c?.key_id ?? "", admin_email: c?.admin_email ?? "", secret: "",
    enabled: c?.enabled ?? true, notes: c?.notes ?? "",
  });
  const [base, setBase] = useState<{ c: Connection | null; etag: string | null }>({ c: existing, etag: null });
  const [form, setForm] = useState<Form>(() => startFrom(existing));
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [stale, setStale] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [version, setVersion] = useState(0);
  // Fields typed into since the form opened (or since "Load the latest version"), and the copy they were typed against.
  const edited = useRef(new Set<keyof Form>());
  const typedAgainst = useRef<Connection | null>(existing);
  // The ETag comes from a fresh read: the list's copy may be older than what is stored. Again after "Load the latest".
  // The form opens at once from the list's copy and can be typed into while that read is on its way: what was typed
  // is kept when it lands (a secret typed in that moment was dropped and the old one tested),
  // unless the read shows someone else changed that field meanwhile. Then their value is shown and the form says so,
  // rather than saving the typed value over theirs under the new ETag. "Load the latest version" forgets what was
  // typed before it, and starts again from what is stored, as it says.
  useEffect(() => {
    if (!existing) return;
    let live = true;
    apiWithEtag<Connection>(`${path}/${existing.id}`)
      .then(({ data, etag }) => {
        if (!live) return;
        const fresh = startFrom(data);
        const opened = startFrom(typedAgainst.current);
        const keep = [...edited.current];
        const theirs = keep.filter((k) => k !== "secret" && fresh[k] !== opened[k]);
        for (const k of theirs) edited.current.delete(k);
        typedAgainst.current = data;
        setBase({ c: data, etag });
        setForm((typed) => {
          const next = { ...fresh };
          for (const k of keep) if (!theirs.includes(k)) (next as Record<keyof Form, unknown>)[k] = typed[k];
          return next;
        });
        setStale(theirs.length > 0);
      })
      .catch((e) => { if (live) setLoadError(problemText(e, "Couldn't load the connection.")); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [existing?.id, version]);
  const set = <K extends keyof Form>(k: K, v: Form[K]) => { edited.current.add(k); setForm((f) => ({ ...f, [k]: v })); };
  const id = (k: string) => `conn-${k}`;
  const secretText = CONNECTION_TEXT[kind];
  // The synthetic tenant is offered only where this server accepts it (auth/me).
  const simulationAllowed = !!useMe().simulation_allowed;
  const shown = Object.fromEntries(Object.entries(errors).map(([k, v]) => [id(k), v]));
  // A stored secret is only sent where it was entered for. When a Halo or Okta address
  // moves (another scheme, host or port) the secret is required again in the same save, and the field says so.
  // Only with a stored secret to protect, as the API decides (its had_secret): a connection without one needs it anyway.
  const moved = !!base.c?.secret_set_on && (kind === "halo" || kind === "okta") && destinationMoved(base.c.base_url ?? "", form.base_url);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (saving) return;
    if (moved && !form.secret.trim()) {
      setErrors({ secret: [`The address changed: enter the ${secretText.secret} for the new address.`] });
      return;
    }
    const initial = startFrom(base.c);
    const body: Record<string, unknown> = {};
    for (const k of FIELDS[kind]) {
      const v = typeof form[k] === "string" ? (form[k] as string).trim() : form[k];
      if (!existing || v !== (typeof initial[k] === "string" ? (initial[k] as string).trim() : initial[k])) body[k] = v;
    }
    if ((kind === "halo" || kind === "okta") && !existing && !form.scope.trim()) delete body.scope; // the API's default scope
    if (!existing || form.secret) body.secret = form.secret;
    if (existing && !Object.keys(body).length) return onDone("Nothing changed.");
    setSaving(true);
    setStale(false);
    try {
      if (existing) {
        const saved = await api<Connection>(`${path}/${existing.id}`, { method: "PATCH", body, headers: base.etag ? { "If-Match": base.etag } : {} });
        onDone(saved.test_current ? `${saved.name} saved.` : `${saved.name} saved. It changed since its last test: test it again before discovery runs.`);
      } else {
        const created = await api<Connection>(path, { method: "POST", body });
        onDone(`${created.name} added. Testing it now…`, created);
      }
    } catch (err) {
      if (err instanceof Problem && err.status === 412 && err.type === PRECONDITION_FAILED) setStale(true);
      else setErrors(err instanceof Problem && Object.keys(err.errors).length ? err.errors : { name: [problemText(err, "Couldn't save the connection.")] });
    } finally {
      setSaving(false);
    }
  }
  if (loadError) return <ErrorBanner message={loadError} />;
  return (
    <form onSubmit={submit} noValidate>
      {stale && (
        <div className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-state-pending-foreground" role="alert">
          <p className="font-semibold">Someone changed this connection since you opened it. Nothing was saved.</p>
          <Button type="button" size="sm" variant="outline" className="mt-2" onClick={() => { edited.current.clear(); setVersion((v) => v + 1); }}>Load the latest version</Button>
        </div>
      )}
      <ErrorSummary errors={shown} />
      <Field id={id("name")} label="Name (required)" errors={shown} help="Connectors and their settings show this name.">
        <Input id={id("name")} value={form.name} maxLength={100} onChange={(e) => set("name", e.target.value)} />
      </Field>
      {kind === "microsoft" ? (
        <>
          <Field id={id("tenant_id")} label="Directory (tenant) ID (required)" errors={shown} help={`A GUID or one of the tenant's domains.${simulationAllowed ? " Enter simulated for the synthetic tenant (no calls to Microsoft)." : ""}`}>
            <Input id={id("tenant_id")} value={form.tenant_id} onChange={(e) => set("tenant_id", e.target.value)} autoComplete="off" spellCheck={false} />
          </Field>
          <Field id={id("client_id")} label="Application (client) ID" errors={shown} help={`A GUID, from the app registration's Overview.${simulationAllowed ? " Any value works for the synthetic tenant." : ""}`}>
            <Input id={id("client_id")} value={form.client_id} onChange={(e) => set("client_id", e.target.value)} autoComplete="off" spellCheck={false} />
          </Field>
        </>
      ) : kind === "okta" ? (
        <>
          <Field id={id("base_url")} label="Okta address (required)" errors={shown} help="Your org's address, such as https://yourcompany.okta.com (not the -admin one), or its custom domain. Private and loopback addresses are refused.">
            <Input id={id("base_url")} value={form.base_url} onChange={(e) => set("base_url", e.target.value)} autoComplete="off" spellCheck={false} />
          </Field>
          <Field id={id("client_id")} label="Client ID (required)" errors={shown} help="The API service app's client ID.">
            <Input id={id("client_id")} value={form.client_id} onChange={(e) => set("client_id", e.target.value)} autoComplete="off" spellCheck={false} />
          </Field>
          <Field id={id("key_id")} label="Key ID" errors={shown} help="The key's kid as Okta shows it. Needed when the app has more than one key; a JWK's own kid is used when empty.">
            <Input id={id("key_id")} value={form.key_id} onChange={(e) => set("key_id", e.target.value)} autoComplete="off" spellCheck={false} />
          </Field>
          <Field id={id("scope")} label="Scope" errors={shown} help="Leave empty for the default, okta.apps.read: all this source reads. Only read-only Okta scopes are accepted.">
            <Input id={id("scope")} value={form.scope} onChange={(e) => set("scope", e.target.value)} autoComplete="off" spellCheck={false} />
          </Field>
        </>
      ) : kind === "google_workspace" ? (
        <>
          <Field id={id("admin_email")} label="Administrator (required)" errors={shown} help="The Workspace administrator the service account acts as: a super administrator, or a role with the Reports privilege.">
            <Input id={id("admin_email")} type="email" value={form.admin_email} onChange={(e) => set("admin_email", e.target.value)} autoComplete="off" spellCheck={false} />
          </Field>
          {base.c && (
            <p className="mb-3 text-sm">
              Service account <span className="font-mono text-xs">{base.c.service_account_email || "—"}</span>, client ID <span className="font-mono text-xs">{base.c.client_id || "—"}</span>: the ID to authorize under domain-wide delegation.
            </p>
          )}
        </>
      ) : (
        <>
          <Field id={id("base_url")} label="Halo address (required)" errors={shown} help="https://yourcompany.haloitsm.com. Loopback, link-local and metadata addresses are refused.">
            <Input id={id("base_url")} value={form.base_url} onChange={(e) => set("base_url", e.target.value)} autoComplete="off" spellCheck={false} />
          </Field>
          <Field id={id("client_id")} label="Client ID (required)" errors={shown}>
            <Input id={id("client_id")} value={form.client_id} onChange={(e) => set("client_id", e.target.value)} autoComplete="off" spellCheck={false} />
          </Field>
          <Field id={id("scope")} label="Scope" errors={shown} help="Leave empty for the default, read:assets: all this source reads.">
            <Input id={id("scope")} value={form.scope} onChange={(e) => set("scope", e.target.value)} autoComplete="off" spellCheck={false} />
          </Field>
        </>
      )}
      {base.c && <p className="mb-1.5 text-sm"><SecretState setOn={base.c.secret_set_on} what={sentenceCase(secretText.secret)} /></p>}
      <Field id={id("secret")} label={!base.c ? `${sentenceCase(secretText.secret)} (required)` : moved ? `New ${secretText.secret} (required)` : `Replace ${secretText.secret}`} errors={shown}
        help={!base.c ? secretText.secretHelp
          : moved ? `The address changed, so the stored ${secretText.secret} isn't sent there: enter it again for the new address.`
            : `Leave empty to keep the stored ${secretText.secret}. It is never shown.`}>
        {secretText.multiline
          ? <Textarea id={id("secret")} rows={6} value={form.secret} onChange={(e) => set("secret", e.target.value)} autoComplete="off" spellCheck={false} className="font-mono text-xs" />
          : <Input id={id("secret")} type="password" value={form.secret} onChange={(e) => set("secret", e.target.value)} autoComplete="new-password" />}
      </Field>
      <label className="mb-3 flex items-center gap-2 text-sm">
        <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} className="h-4 w-4 accent-primary" />
        On (connectors may use it)
      </label>
      <Field id={id("notes")} label="Notes" errors={shown} help="For Admins: who owns the app registration, when the secret expires.">
        <Textarea id={id("notes")} value={form.notes} maxLength={5000} onChange={(e) => set("notes", e.target.value)} />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        {/* An edit is saved with the ETag of a fresh read: wait for it. */}
        <Button type="submit" disabled={saving || (!!existing && !base.etag && !loadError)}>{saving ? "Saving…" : existing && !base.etag ? "Loading…" : existing ? "Save connection" : "Add and test"}</Button>
      </DialogFooter>
    </form>
  );
}

// ---- delete ---------------------------------------------------------------------------------------------------------

function DeleteConnectionDialog({ kind, item, onClose, onDone }: { kind: ConnectionKind; item: Connection | null; onClose: () => void; onDone: (text: string) => void }) {
  const open = item !== null;
  const returnFocus = useReturnFocus(open);
  const [refusal, setRefusal] = useResetOn<{ detail: string; connectors: { id: string; name: string }[]; tickets: { id: string; name: string }[] } | null>(open ? item.id : null, null);
  const [busy, setBusy] = useState(false);
  const [last, setLast] = useState(item);
  if (item && item !== last) setLast(item);
  const shown = item ?? last;
  // Read with its ETag as the confirmation opens; the delete sends it as If-Match.
  const opened = useOpenedRecord(item ? `${connectionsPath(kind)}/${item.id}` : null);
  async function remove() {
    if (!item) return;
    setBusy(true);
    try {
      await api(`${connectionsPath(kind)}/${item.id}`, { method: "DELETE", headers: ifMatch(await opened.etag()) });
      onDone(`${item.name} deleted.`);
    } catch (e) {
      if (isStaleWrite(e)) { setRefusal({ detail: staleDeleteText("connection"), connectors: [], tickets: [] }); return; }
      const users = e instanceof Problem && Array.isArray(e.body.connectors) ? (e.body.connectors as { id: string; name: string }[]) : [];
      // A Halo connection a ticket connection signs in through is refused too, naming it.
      const tickets = e instanceof Problem && Array.isArray(e.body.ticket_connections) ? (e.body.ticket_connections as { id: string; name: string }[]) : [];
      setRefusal({ detail: problemText(e, "Couldn't delete it."), connectors: users, tickets });
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Delete {shown?.name}?</DialogTitle>
          <DialogDescription>Its secret is deleted with it. A connection can't be deleted while a connector signs in through it.</DialogDescription>
        </DialogHeader>
        {refusal && (
          <div className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">
            <p>{refusal.detail}</p>
            {refusal.connectors.length > 0 && (
              <ul className="mt-1 ml-5 list-disc">
                {refusal.connectors.map((c) => <li key={c.id}><Link className="underline" to={`/discovery/connectors/${c.id}`}>{c.name}</Link></li>)}
              </ul>
            )}
            {refusal.tickets.length > 0 && (
              <ul className="mt-1 ml-5 list-disc" aria-label="Ticket connections using it">
                {refusal.tickets.map((c) => <li key={c.id}><Link className="underline" to="/admin/ticket-connections">Ticket connection {c.name}</Link></li>)}
              </ul>
            )}
          </div>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={busy} onClick={() => void remove()}>{busy ? "Deleting…" : `Delete ${shown?.name ?? ""}`}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

const sentenceCase = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);
