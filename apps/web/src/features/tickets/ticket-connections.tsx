/** Admin › Ticket connections (capability `ticket_links`, Admin only, reads included): how Softwarden
 *  reads ticket statuses and, when an Admin allows it, creates tickets. One connection per system: Jira (site address,
 *  account email, API token), ServiceNow (instance address, user name, password) and HaloITSM (an existing Halo
 *  connection, capability `discovery.halo`). The secret is write-only: "set on DATE" and a Replace action, never shown.
 *  Creating tickets is off until an Admin turns it on (with Jira's project key and issue type, ServiceNow's table or
 *  Halo's ticket type). Test signs in and makes one read; its result is shown in words. Laid out like Discovery ›
 *  Connections. ServiceNow and Halo are preview features (launch scope): their sections appear only while the
 *  installation turns preview features on (capability `preview`); otherwise the page offers Jira alone. Loaded on
 *  first use. */
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, apiWithEtag, PRECONDITION_FAILED, Problem, when, type Connection as HaloConnection, type ProbeResult } from "@/api";
import { AdminOnly, ErrorBanner, ErrorSummary, Field, Loading, PageHead, Select, Textarea, useReturnFocus } from "@/components/app";
import { chip, DetailList, DetailRow, GapChip, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Unavailable } from "@/components/url-filters";
import { ProbeResultNote, TestStateChip, unavailableReason } from "@/features/discovery/discovery-ui";
import { SecretState } from "@/features/notify/notify-ui";
import type { Schemas } from "@/lib/openapi";
import { destinationMoved } from "@/lib/destination";
import { problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { ANY_REVISION, ifMatch, isStaleWrite, staleDeleteText, useOpenedRecord } from "@/lib/if-match";
import { useMe } from "@/lib/session";

type TicketConnection = Schemas["TicketConnection"];
/** An `available` connection: the API nulls a connection's settings only when it isn't available (one kept from when
 *  the installation's preview features were on), so on these they are present. */
type UsableConnection = TicketConnection & { test_current: boolean };
const usable = (c: TicketConnection): c is UsableConnection => c.available;
type System = Schemas["TicketConnectionSystemEnum"];
const PATH = "ticket-connections";

const SYSTEMS: { id: System; label: string; secret: string; user: string; site: string; siteHelp: string; target: string; targetHelp: string; about: string; preview?: true;
  /** Added to siteHelp only where the server accepts the simulators (`auth/me.simulation_allowed`). */
  simulatedHelp?: string }[] = [
  {
    id: "jira", label: "Jira", secret: "API token", user: "Account email", site: "Jira site address",
    siteHelp: "https://yourcompany.atlassian.net.", simulatedHelp: "https://jira.simulated.invalid reads a synthetic Jira.",
    target: "Project key", targetHelp: "The project new tickets go to, such as ITOPS. Needed to create tickets.",
    about: "Reads issue statuses by key with an Atlassian API token of the account below.",
  },
  {
    id: "servicenow", label: "ServiceNow", preview: true, secret: "Password", user: "User name", site: "ServiceNow instance address",
    siteHelp: "https://yourcompany.service-now.com.", simulatedHelp: "https://servicenow.simulated.invalid reads a synthetic instance.",
    target: "Table", targetHelp: "The table new tickets go to, such as incident.",
    about: "Reads incidents, changes and requests by number as the user below (basic authentication).",
  },
  {
    id: "halo", label: "HaloITSM", preview: true, secret: "", user: "", site: "", siteHelp: "",
    target: "Ticket type ID", targetHelp: "The Halo ticket type new tickets get, a number.",
    about: "Signs in through one of your Halo connections. Its scope must include read:tickets, and edit:tickets to create tickets.",
  },
];
const info = (s: System) => SYSTEMS.find((x) => x.id === s)!;
/** The systems this installation offers: Jira, and ServiceNow and Halo only with the `preview` capability. */
const systemsFor = (capabilities: string[]) => SYSTEMS.filter((s) => !s.preview || capabilities.includes("preview"));
/** A secret's name inside a sentence: "API token" keeps its capitals, "Password" becomes "password". */
const lower = (name: string) => (name === "API token" ? name : name.toLowerCase());

export function TicketConnectionsPage() {
  return (
    <AdminOnly title="Ticket connections" what="how Softwarden reads ticket statuses from your ticket systems">
      <TicketConnectionsGate />
    </AdminOnly>
  );
}

function TicketConnectionsGate() {
  const me = useMe();
  if (!me.capabilities.includes("ticket_links")) return <><PageHead title="Ticket connections" /><Unavailable what="Ticket links" testId="ticket-connections-unavailable" /></>;
  return <TicketConnections />;
}

function TicketConnections() {
  const me = useMe();
  const q = useApiQuery<TicketConnection[]>(PATH);
  const invalidate = useInvalidate();
  const [notice, setNotice] = useState("");
  const [editing, setEditing] = useState<{ system: System; item: TicketConnection | null } | null>(null);
  const [deleting, setDeleting] = useState<TicketConnection | null>(null);
  const [testing, setTesting] = useState("");
  const [outcome, setOutcome] = useState<{ name: string; result: ProbeResult } | null>(null);
  const [testError, setTestError] = useState("");
  const refresh = () => invalidate(PATH);
  async function test(c: { id: string; name: string }) {
    setTesting(c.id); setOutcome(null); setTestError("");
    try {
      const r = await api<TicketConnection & { result: ProbeResult }>(`${PATH}/${c.id}/test`, { method: "POST" });
      setOutcome({ name: r.name, result: r.result });
      await refresh();
    } catch (e) {
      setTestError(`Couldn't test ${c.name}: ${problemText(e, "something went wrong. Try again.")}`);
    } finally {
      setTesting("");
    }
  }
  /** The list reloads before the dialog closes, so the opener a delete or an add replaced is gone by then and the
   *  dialog's fallback takes focus: the section's Add button after a delete, its heading after an add. */
  async function done(text: string, created?: TicketConnection) {
    setNotice(text); setOutcome(null); setTestError("");
    await refresh();
    setEditing(null); setDeleting(null);
    if (created) void test(created);
  }
  const rows = q.data;
  // Connections kept from when the installation's preview features were on (`available` false): only deletable.
  const kept = (rows ?? []).filter((r) => !r.available);
  const systems = systemsFor(me.capabilities);
  const names = systems.length > 1 ? `${systems.slice(0, -1).map((s) => s.label).join(", ")} and ${systems[systems.length - 1].label}` : systems[0].label;
  return (
    <>
      <PageHead title="Ticket connections" desc={`How Softwarden reads the status of linked ${names} tickets, and creates tickets when you allow it. One connection per system; secrets are stored encrypted and never shown again.`} />
      <p className="mb-4 max-w-prose text-sm text-muted-foreground">
        Test a connection after creating or changing it: statuses are read only through a connection that passed its test since its last change. Creating tickets stays off until you turn it on here; every attempt is recorded in the audit log.
      </p>
      <SuccessNote text={notice} />
      {q.isError && <ErrorBanner message={problemText(q.error, "Couldn't load the ticket connections.")} onRetry={() => void q.refetch()} />}
      {!rows && !q.isError && <Loading what="ticket connections" rows={3} />}
      <div className="mb-3" role="status" data-testid="ticket-test-result">
        {outcome && <ProbeResultNote name={outcome.name} result={outcome.result} />}
        {testError && <p className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2"><span aria-hidden="true">! </span>{testError}</p>}
      </div>
      {rows && (
        <div className="grid gap-4">
          {systems.map((s) => {
            const c = rows.filter(usable).find((r) => r.system === s.id) ?? null;
            const haloOff = s.id === "halo" && !me.capabilities.includes("discovery.halo");
            return (
              <section key={s.id} aria-labelledby={`tc-${s.id}-h`} className="overflow-hidden rounded-lg border bg-card" data-system={s.id}>
                <div className="flex flex-wrap items-center gap-2.5 border-b px-4 py-2.5">
                  <h2 id={`tc-${s.id}-h`} tabIndex={-1} className="font-display text-section font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring">{s.label}</h2>
                  {c && <TestStateChip c={c} />}
                  <span className="ml-auto inline-flex flex-wrap gap-2">
                    {c && (
                      <>
                        <Button size="sm" variant="outline" disabled={testing === c.id} onClick={() => void test(c)}>{testing === c.id ? "Testing…" : "Test"}<span className="sr-only"> {c.name}</span></Button>
                        <Button size="sm" variant="outline" onClick={() => setEditing({ system: s.id, item: c })}>Edit<span className="sr-only"> {c.name}</span></Button>
                        <Button size="sm" variant="outline" onClick={() => setDeleting(c)}>Delete<span className="sr-only"> {c.name}</span></Button>
                      </>
                    )}
                    {!c && !haloOff && <Button id={`tc-${s.id}-add`} size="sm" variant="outline" onClick={() => setEditing({ system: s.id, item: null })}>Add a {s.label} connection</Button>}
                  </span>
                </div>
                {haloOff && !c ? (
                  <div className="p-4"><Unavailable what="Halo ticket links" reason={`They sign in through a Halo connection. ${unavailableReason("Halo discovery")}`} /></div>
                ) : !c ? (
                  <p className="px-4 py-3 text-sm text-muted-foreground">No {s.label} connection. {s.about} Until one is added, links to {s.label} tickets keep a status set by hand.</p>
                ) : <ConnectionDetails c={c} />}
              </section>
            );
          })}
          {kept.length > 0 && (
            <section aria-labelledby="tc-kept-h" className="overflow-hidden rounded-lg border border-dashed border-border-strong bg-muted" data-testid="kept-connections">
              <h2 id="tc-kept-h" tabIndex={-1} className="border-b px-4 py-2.5 font-display text-section font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring">Kept from a preview install</h2>
              <p className="px-4 pt-3 text-sm">
                <span aria-hidden="true">⊘ </span>These ticket connections were added while this installation's preview features were on. They are kept as they are and never used; you can delete them, for example to delete the Halo connection one signs in through.
              </p>
              <ul className="m-0 list-none p-0" aria-label="Kept ticket connections">
                {kept.map((c) => (
                  <li key={c.id} className="flex flex-wrap items-center gap-2.5 px-4 py-2.5" data-kept={c.system}>
                    <span className="font-semibold">{c.name}</span>
                    <span className="text-sm text-muted-foreground">{info(c.system).label}</span>
                    <Button size="sm" variant="outline" className="ml-auto" onClick={() => setDeleting(c)}>Delete<span className="sr-only"> {c.name}</span></Button>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}
      {editing && <ConnectionDialog system={editing.system} item={editing.item} onClose={() => setEditing(null)} onDone={done} />}
      {deleting && <DeleteDialog item={deleting} onClose={() => setDeleting(null)} onDone={(t) => done(t)} />}
    </>
  );
}

function ConnectionDetails({ c }: { c: TicketConnection }) {
  const s = info(c.system);
  const halo = c.halo_connection;
  const note = c.test_current ? "" : c.last_test === null ? "Test it before statuses are read." : c.last_test && !c.last_test.ok ? "Fix it and test again; statuses aren't read until it passes." : "Test it again; statuses aren't read until it passes.";
  return (
    <DetailList>
      <DetailRow label="Name">
        {c.name}
        {!c.enabled && <span className="block text-sm text-muted-foreground">Turned off: statuses aren't read and tickets aren't created</span>}
        {c.simulated && <span className="block text-sm text-muted-foreground">Synthetic data (no network)</span>}
      </DetailRow>
      {c.system === "halo" ? (
        <DetailRow label="Halo connection">{halo ? <Link to="/discovery/connections?tab=halo" className="underline">{halo.name}</Link> : <GapChip label="No Halo connection" />}</DetailRow>
      ) : (
        <>
          <DetailRow label={s.site}><span className="[overflow-wrap:anywhere]">{c.base_url}</span></DetailRow>
          <DetailRow label={s.user}>{c.username || <span className="text-muted-foreground">Not set</span>}</DetailRow>
          <DetailRow label={s.secret}>
            <span data-testid="secret-state"><SecretState setOn={c.secret_set_on} what={s.secret} /></span>
            <span className="block text-sm text-muted-foreground">Never shown. Replace it from Edit.</span>
          </DetailRow>
        </>
      )}
      <DetailRow label="Test">
        <span className="block text-sm">{note || "Statuses are read through it."}</span>
        {c.last_test && (
          <span className="block text-sm text-muted-foreground">
            Last test {when(c.last_test.at)}{c.last_test.by ? ` by ${c.last_test.by.name}` : ""}: {c.last_test.ok ? "passed" : (c.last_test.problem_label || "failed")}.
          </span>
        )}
      </DetailRow>
      <DetailRow label="Creating tickets">
        <span data-testid="create-state">
          {c.create_enabled
            ? <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>On</span>
            : <span className={chip({ state: "neutral" })}><span aria-hidden="true">–</span>Off</span>}
        </span>
        <span className="block text-sm text-muted-foreground">
          {c.create_enabled
            ? [c.create_target ? `${s.target} ${c.create_target}` : `No ${s.target.toLowerCase()} set`, c.system === "jira" && c.create_issue_type ? `issue type ${c.create_issue_type}` : ""].filter(Boolean).join(", ") + ". Editors and Admins can create tickets from assessments and renewals."
            : "Editors and Admins can link tickets but not create them."}
        </span>
        {c.create_enabled && c.system === "jira" && !c.create_target && <span className="mt-1 block"><GapChip label="No project key" /></span>}
      </DetailRow>
      {c.notes && <DetailRow label="Notes"><span className="whitespace-pre-line">{c.notes}</span></DetailRow>}
    </DetailList>
  );
}

// ---- create and edit ---------------------------------------------------------------------------------------------------

type Form = { name: string; base_url: string; username: string; secret: string; halo_connection: string; enabled: boolean; create_enabled: boolean; create_target: string; create_issue_type: string; notes: string };
const FIELDS: Record<System, (keyof Form)[]> = {
  jira: ["name", "base_url", "username", "enabled", "create_enabled", "create_target", "create_issue_type", "notes"],
  servicenow: ["name", "base_url", "username", "enabled", "create_enabled", "create_target", "notes"],
  halo: ["name", "halo_connection", "enabled", "create_enabled", "create_target", "notes"],
};

function ConnectionDialog({ system, item, onClose, onDone }: { system: System; item: TicketConnection | null; onClose: () => void; onDone: (text: string, created?: TicketConnection) => Promise<void> }) {
  // After an add, the Add button that opened it is gone: focus goes to the system's heading (Test is busy testing).
  const returnFocus = useReturnFocus(true, () => document.getElementById(`tc-${system}-h`));
  const s = info(system);
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>{item ? `Edit ${item.name}` : `Add a ${s.label} connection`}</DialogTitle>
          <DialogDescription>
            {item ? "A change to how it signs in needs a new test before statuses are read." : "Saved and then tested at once."}{system !== "halo" && ` The ${lower(s.secret)} is stored encrypted and never shown again.`}
          </DialogDescription>
        </DialogHeader>
        <ConnectionForm system={system} existing={item} onCancel={onClose} onDone={onDone} />
      </DialogContent>
    </Dialog>
  );
}

function ConnectionForm({ system, existing, onCancel, onDone }: { system: System; existing: TicketConnection | null; onCancel: () => void; onDone: (text: string, created?: TicketConnection) => Promise<void> }) {
  const s = info(system);
  const me = useMe();
  const startFrom = (c: TicketConnection | null): Form => ({
    name: c?.name ?? s.label, base_url: c?.base_url ?? "", username: c?.username ?? "", secret: "", halo_connection: c?.halo_connection?.id ?? "",
    enabled: c?.enabled ?? true, create_enabled: c?.create_enabled ?? false, create_target: c?.create_target ?? "", create_issue_type: c?.create_issue_type ?? "", notes: c?.notes ?? "",
  });
  const [base, setBase] = useState<{ c: TicketConnection | null; etag: string | null }>({ c: existing, etag: null });
  const [form, setForm] = useState<Form>(() => startFrom(existing));
  const [replacing, setReplacing] = useState(!existing);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [failure, setFailure] = useState("");
  const [stale, setStale] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [version, setVersion] = useState(0);
  const halos = useApiQuery<HaloConnection[]>("connections/halo", { enabled: system === "halo" });
  // Fields typed into since the form opened (or since "Load the latest version"), and the copy they were typed against.
  const edited = useRef(new Set<keyof Form>());
  const typedAgainst = useRef<TicketConnection | null>(existing);
  // The ETag comes from a fresh read: the list's copy may be older than what is stored. Again after "Load the latest".
  // What is typed before that read lands is kept (it was dropped, as in discovery/connections.tsx),
  // unless the read shows someone else changed that field meanwhile: then their value is shown and the form says
  // so, rather than saving the typed value over theirs under the new ETag. "Load the latest version" forgets what was
  // typed before it.
  useEffect(() => {
    if (!existing) return;
    let live = true;
    apiWithEtag<TicketConnection>(`${PATH}/${existing.id}`)
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
  const id = (k: string) => `tc-${k}`;
  const shown = Object.fromEntries(Object.entries(errors).map(([k, v]) => [id(k), v]));
  // A stored secret is only sent where it was entered for, so a new address (another
  // scheme, host or port) needs it again in the same save. The secret field opens then, and when the API refuses on it.
  const moved = !!base.c && system !== "halo" && destinationMoved(base.c.base_url ?? "", form.base_url);
  const secretOpen = !base.c || replacing || moved;
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (saving) return;
    const initial = startFrom(base.c);
    const body: Record<string, unknown> = {};
    for (const k of FIELDS[system]) {
      const v = typeof form[k] === "string" ? (form[k] as string).trim() : form[k];
      if (!existing || v !== (typeof initial[k] === "string" ? (initial[k] as string).trim() : initial[k])) body[k] = v;
    }
    if (!existing) body.system = system;
    if (system === "halo" && !form.halo_connection) delete body.halo_connection;
    if (system !== "halo" && secretOpen && (!existing || form.secret)) body.secret = form.secret;
    if (existing && !Object.keys(body).length) { await onDone("Nothing changed."); return; }
    setSaving(true); setStale(false); setFailure(""); setErrors({});
    try {
      if (existing) {
        const saved = await api<TicketConnection>(`${PATH}/${existing.id}`, { method: "PATCH", body, headers: base.etag ? { "If-Match": base.etag } : {} });
        await onDone(saved.test_current ? `${saved.name} saved.` : `${saved.name} saved. It changed since its last test: test it again before statuses are read.`);
      } else {
        const created = await api<TicketConnection>(PATH, { method: "POST", body });
        await onDone(`${created.name} added. Testing it now…`, created);
      }
    } catch (err) {
      if (err instanceof Problem && err.status === 412 && err.type === PRECONDITION_FAILED) setStale(true);
      else if (err instanceof Problem && Object.keys(err.errors).length) {
        // A refusal on the secret opens its field, so the summary's link lands on it.
        if (err.errors.secret) setReplacing(true);
        setErrors(err.errors);
      }
      else setFailure(`${problemText(err, "Couldn't save the connection.")} Nothing was saved.`);
    } finally {
      setSaving(false);
    }
  }
  if (loadError) return <ErrorBanner message={loadError} />;
  const haloOptions = halos.data ?? [];
  return (
    <form onSubmit={submit} noValidate aria-label={`${s.label} connection`}>
      {stale && (
        <div className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-state-pending-foreground" role="alert">
          <p className="font-semibold">Someone changed this connection since you opened it. Nothing was saved.</p>
          <Button type="button" size="sm" variant="outline" className="mt-2" onClick={() => { edited.current.clear(); setVersion((v) => v + 1); }}>Load the latest version</Button>
        </div>
      )}
      {failure && <ErrorBanner message={failure} />}
      <ErrorSummary errors={shown} />
      <Field id={id("name")} label="Name (required)" errors={shown}>
        <Input id={id("name")} value={form.name} maxLength={100} onChange={(e) => set("name", e.target.value)} />
      </Field>
      {system === "halo" ? (
        <Field id={id("halo_connection")} label="Halo connection (required)" errors={shown} help="The Halo connection it signs in through. Its scope must include read:tickets, and edit:tickets to create tickets.">
          <Select id={id("halo_connection")} value={form.halo_connection} onChange={(e) => set("halo_connection", e.target.value)}>
            <option value="">{halos.isLoading ? "Loading Halo connections…" : haloOptions.length ? "Choose a Halo connection" : "No Halo connection yet"}</option>
            {haloOptions.map((h) => <option key={h.id} value={h.id}>{h.name} ({h.base_url})</option>)}
          </Select>
        </Field>
      ) : (
        <>
          <Field id={id("base_url")} label={`${s.site} (required)`} errors={shown} help={me.simulation_allowed && s.simulatedHelp ? `${s.siteHelp} ${s.simulatedHelp}` : s.siteHelp}>
            <Input id={id("base_url")} type="url" inputMode="url" value={form.base_url} onChange={(e) => set("base_url", e.target.value)} autoComplete="off" spellCheck={false} />
          </Field>
          <Field id={id("username")} label={`${s.user} (required)`} errors={shown}>
            <Input id={id("username")} value={form.username} onChange={(e) => set("username", e.target.value)} autoComplete="off" spellCheck={false} />
          </Field>
          {base.c && !secretOpen ? (
            <div className="mb-3 flex flex-col gap-1.5">
              <span className="text-sm font-medium">{s.secret}</span>
              <span className="flex flex-wrap items-center gap-2" data-testid="secret-state">
                <SecretState setOn={base.c.secret_set_on} what={s.secret} />
                <span className="text-sm text-muted-foreground">Never shown.</span>
                <Button type="button" size="sm" variant="outline" onClick={() => setReplacing(true)}>Replace<span className="sr-only"> the {lower(s.secret)}</span></Button>
              </span>
            </div>
          ) : (
            <>
              <Field id={id("secret")} label={!base.c ? `${s.secret} (required)` : moved ? `New ${lower(s.secret)} (required)` : `New ${lower(s.secret)}`} errors={shown}
                help={!base.c ? "Stored encrypted and never shown again."
                  : moved ? `The address changed, so the stored ${lower(s.secret)} isn't sent there: enter it again for the new address.`
                    : `Replaces the stored ${lower(s.secret)} when you save.`}>
                <Input id={id("secret")} type="password" value={form.secret} onChange={(e) => set("secret", e.target.value)} autoComplete="new-password" />
              </Field>
              {base.c && !moved && <Button type="button" size="sm" variant="outline" className="-mt-1 mb-3" onClick={() => { setReplacing(false); setErrors((e) => { const next = { ...e }; delete next.secret; return next; }); set("secret", ""); }}>Keep the stored {lower(s.secret)}</Button>}
            </>
          )}
        </>
      )}
      <label className="mb-3 flex min-h-6 items-center gap-2 text-sm">
        <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} className="h-4 w-4 accent-primary" />
        On (statuses are read through it)
      </label>
      <fieldset className="mb-3 rounded-md border px-3 pb-1 pt-2">
        <legend className="px-1 text-sm font-semibold">Creating tickets</legend>
        <label className="mb-3 flex min-h-6 items-center gap-2 text-sm">
          <input type="checkbox" checked={form.create_enabled} onChange={(e) => set("create_enabled", e.target.checked)} className="h-4 w-4 accent-primary" />
          Let Editors and Admins create {s.label} tickets from assessments and renewals
        </label>
        <Field id={id("create_target")} label={s.target} errors={shown} help={s.targetHelp}>
          <Input id={id("create_target")} value={form.create_target} onChange={(e) => set("create_target", e.target.value)} autoComplete="off" spellCheck={false} />
        </Field>
        {system === "jira" && (
          <Field id={id("create_issue_type")} label="Issue type" errors={shown} help="Such as Task.">
            <Input id={id("create_issue_type")} value={form.create_issue_type} onChange={(e) => set("create_issue_type", e.target.value)} autoComplete="off" />
          </Field>
        )}
      </fieldset>
      <Field id={id("notes")} label="Notes" errors={shown} help="For Admins: who owns the account, when the token expires.">
        <Textarea id={id("notes")} value={form.notes} maxLength={5000} onChange={(e) => set("notes", e.target.value)} />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={saving || (!!existing && !base.etag)}>{saving ? "Saving…" : existing && !base.etag ? "Loading…" : existing ? "Save connection" : "Add and test"}</Button>
      </DialogFooter>
    </form>
  );
}

function DeleteDialog({ item, onClose, onDone }: { item: TicketConnection; onClose: () => void; onDone: (text: string) => Promise<void> }) {
  // Where focus goes once the Delete button that opened this dialog is gone (the list reloads before it closes), the
  // first that exists:
  //  1. the system's "Add a … connection" button, which replaces a deleted usable connection;
  //  2. the system's section heading, when no Add button is shown (Halo without discovery.halo);
  //  3. the "Kept from a preview install" heading: a kept connection has no section of its own, and others are left;
  //  4. the Jira heading, always on the page, once the last kept connection is gone and the kept section with it.
  const returnFocus = useReturnFocus(true, () => document.getElementById(`tc-${item.system}-add`) ?? document.getElementById(`tc-${item.system}-h`) ?? document.getElementById("tc-kept-h") ?? document.getElementById("tc-jira-h"));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const s = info(item.system);
  // Read with its ETag as the confirmation opens; the delete sends it as If-Match. A connection kept from a
  // preview install can't be read (its system is off) or changed by anyone, only deleted: any revision.
  const opened = useOpenedRecord(item.available ? `${PATH}/${item.id}` : null);
  async function remove() {
    setBusy(true); setError("");
    try {
      await api(`${PATH}/${item.id}`, { method: "DELETE", headers: item.available ? ifMatch(await opened.etag()) : ANY_REVISION });
      await onDone(`${item.name} deleted. Links to ${s.label} tickets stay, with their statuses set by hand from now on.`);
    } catch (e) {
      setError(isStaleWrite(e) ? staleDeleteText("connection") : `${problemText(e, "Couldn't delete it.")} Nothing was changed.`);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Delete {item.name}?</DialogTitle>
          <DialogDescription>
            {item.system === "halo" ? "The Halo connection it signs in through stays." : `Its ${lower(s.secret)} is deleted with it.`} Links to {s.label} tickets stay; their statuses are no longer read, and tickets can't be created there.
          </DialogDescription>
        </DialogHeader>
        {error && <ErrorBanner message={error} />}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={busy} onClick={() => void remove()}>{busy ? "Deleting…" : `Delete ${item.name}`}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
