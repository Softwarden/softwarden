/** Discovery › a connector: its status (connection and whether it is tested, the queued or running
 *  run, the last run), its settings and scope, its permissions and last access check, and its recent runs. Admins
 *  change the settings, check access again, run it (not while a run is queued or running) and delete it; other roles
 *  read the same page without those controls. The page asks again every few seconds while a run is queued or running.
 *  Loaded on first use. */
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, apiWithEtag, Problem, when, type Connection, type Connector, type ConnectorRun, type UserRef } from "@/api";
import { StaleBanner } from "@/features/money/contract-ui";
import { ifMatch, isStaleWrite, staleDeleteText, useOpenedRecord } from "@/lib/if-match";
import { Breadcrumb, ErrorBanner, ErrorSummary, Field, isNotFound, Loading, NotFound, PageHead, Select, TableRegion, useReturnFocus } from "@/components/app";
import { DetailList, DetailRow, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { useMe } from "@/lib/session";
import { connectionsPath } from "@/features/discovery/connections";
import {
  AccessCheckResult, changesText, CONNECTION_LABEL, ConnectionLine, connectionsLink, docLink, durationText, PermissionsTable, RunStatusChip, runSentence, seenKept,
  sourceInfo, TRIGGER_LABEL, untestedText,
} from "@/features/discovery/discovery-ui";
import { scopeBody, scopeErrors, ScopeFields, ScopeSummary, scopeValues, type ScopeValues } from "@/features/discovery/scope-form";
import { OnOffChip } from "@/features/notify/notify-ui";

const active = (c: Connector) => !!c.active_run;

export function ConnectorSettingsPage() {
  const { id = "" } = useParams();
  return <ConnectorPage key={id} id={id} />;
}

function ConnectorPage({ id }: { id: string }) {
  const me = useMe();
  const admin = me.role === "admin";
  const q = useApiQuery<Connector>(`connectors/${id}`, { refetchInterval: (query) => (query.state.data && active(query.state.data as Connector) ? 3000 : false) });
  const runs = useApiQuery<ConnectorRun[]>(`connectors/${id}/runs`, { refetchInterval: q.data && active(q.data) ? 3000 : false });
  const invalidate = useInvalidate();
  const [notice, setNotice] = useState("");
  const [refusal, setRefusal] = useState("");
  const [checking, setChecking] = useState(false);
  const [starting, setStarting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [accepting, setAccepting] = useState(false);
  const c = q.data;
  // When the run in progress ends, the recent runs (no longer asked again) are read once more.
  const activeId = c?.active_run?.id ?? "";
  const previous = useRef(activeId);
  useEffect(() => {
    if (previous.current && !activeId) void invalidate(`connectors/${id}/runs`, "discovery/runs");
    previous.current = activeId;
  }, [activeId]); // eslint-disable-line react-hooks/exhaustive-deps
  if (q.isError && isNotFound(q.error)) return <NotFound what="connector" back={{ to: "/discovery", label: "Discovery" }} trail={[{ to: "/discovery", label: "Discovery" }]} />;
  if (q.isError) {
    return (
      <>
        <Breadcrumb trail={[{ to: "/discovery", label: "Discovery" }]} current="Connector" />
        <PageHead title="Connector" />
        <ErrorBanner message={problemText(q.error, "Couldn't load the connector.")} onRetry={() => void q.refetch()} />
      </>
    );
  }
  if (!c) return <Loading what="the connector" rows={3} />;
  const info = sourceInfo(c.source);
  const reload = () => invalidate(`connectors/${id}`, "connectors", "discovery/runs");
  const refresh = () => void reload();
  async function runNow(acceptEmpty = false) {
    setStarting(true);
    setRefusal("");
    setNotice("");
    try {
      await api(`connectors/${id}/run`, { method: "POST", ...(acceptEmpty ? { body: { accept_empty_listing: true } } : {}) });
      setNotice(acceptEmpty
        ? "Run queued, accepting an empty listing this once. The worker starts it within a minute; this page updates on its own."
        : "Run queued. The worker starts it within a minute; this page updates on its own.");
      // Reloaded before going on, so a dialog that started it closes onto the page as it is now (its opener may be gone).
      await reload();
    } catch (e) {
      setRefusal(`Not started: ${problemText(e, "couldn't start the run.")}`);
    } finally {
      setStarting(false);
    }
  }
  async function checkAccess() {
    setChecking(true);
    setRefusal("");
    setNotice("");
    try {
      const r = await api<Connector>(`connectors/${id}/access-check`, { method: "POST" });
      setNotice(r.access_check?.ok ? "Access checked: every required permission works." : `Access checked: ${r.access_check?.missing.length ? `missing ${r.access_check.missing.join(", ")}` : "a step failed"}. See Access below.`);
      refresh();
    } catch (e) {
      setRefusal(problemText(e, "Couldn't run the access check."));
    } finally {
      setChecking(false);
    }
  }
  const runLabel = c.active_run ? (c.active_run.status === "queued" ? "Run queued" : "Running") : starting ? "Queueing…" : "Run now";
  const untested = c.connection && !c.connection.test_current;
  // The empty-listing guard refused the last run. An Admin may run it once accepting that.
  const emptyRefused = !c.active_run && c.last_run?.status === "failed" && c.last_run.failure_code === "empty_listing";
  return (
    <>
      <Breadcrumb trail={[{ to: "/discovery", label: "Discovery" }]} current={c.name} />
      <PageHead title={c.name} desc={<>{c.source_label}{c.simulated && " · Simulated (synthetic data)"}. Read-only: Softwarden lists what it finds as candidates and never changes anything in the source.</>}>
        {admin && c.available && <Button onClick={() => void runNow()} disabled={starting || !!c.active_run} aria-describedby={c.active_run ? "run-state" : undefined}>{runLabel}</Button>}
      </PageHead>
      <SuccessNote text={notice} />
      {refusal && <p className="mb-4 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert"><span aria-hidden="true">! </span>{refusal}</p>}
      {!c.available && (
        <p className="mb-4 rounded-md border border-dashed border-border-strong bg-muted px-3 py-2 text-sm" data-testid="connector-unavailable">
          <span aria-hidden="true">⊘ </span><span className="font-semibold">{c.source_label} isn't available here.</span> This connector doesn't run and can only be turned off or deleted. What it found stays.
        </p>
      )}
      {c.available && emptyRefused && (
        <div className="mb-4 max-w-3xl rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-state-pending-foreground" data-testid="empty-listing">
          <p><span aria-hidden="true">◐ </span><span className="font-semibold">The last run changed nothing: a listing came back empty.</span> This connector still has what it found earlier, so an empty answer is more likely a revoked consent or a fault in the source than everything being deleted.</p>
          {admin
            ? <>
                <p className="mt-1">If those resources really were deleted in the source, run it once accepting the empty listing. Later runs are checked again.</p>
                <div className="mt-2"><Button type="button" size="sm" variant="outline" onClick={() => setAccepting(true)} disabled={starting}>Run once, accepting an empty listing…</Button></div>
              </>
            : <p className="mt-1">An Admin can check the connection and run it again.</p>}
        </div>
      )}
      {c.available && untested && (
        <p className="mb-4 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-state-pending-foreground" data-testid="untested">
          <span aria-hidden="true">◐ </span>{untestedText(c.connection!)}
          {admin && <> <Link className="underline" to={connectionsLink(c.connection!.kind)}>Open Connections</Link></>}
        </p>
      )}
      <section aria-labelledby="status-h" className="mb-6 max-w-3xl rounded-lg border bg-card">
        <h2 id="status-h" className="border-b px-4 py-2.5 font-display text-section font-semibold">Status</h2>
        <DetailList>
          <DetailRow label="State"><OnOffChip on={c.enabled} offText="Turned off" /></DetailRow>
          <DetailRow label="Connection"><ConnectionLine c={c.connection} admin={admin} /></DetailRow>
          <DetailRow label="Run in progress">
            <span id="run-state">
              {c.active_run
                ? <span className="inline-flex flex-wrap items-center gap-2"><RunStatusChip status={c.active_run.status} />{runSentence(c.active_run)}</span>
                : <span className="text-muted-foreground">None</span>}
            </span>
          </DetailRow>
          <DetailRow label="Last run">
            {c.last_run
              ? <span>{runSentence(c.last_run)} <Link className="underline" to={`/discovery/runs/${c.last_run.id}`}>Run details</Link></span>
              : <span className="text-muted-foreground">Not run yet.</span>}
          </DetailRow>
          <DetailRow label="Candidates">
            {c.candidates.new} new, {c.candidates.changed} changed. <Link className="underline" to={`/discovery?connector=${c.id}`}>Review candidates<span className="sr-only"> from {c.name}</span></Link>
          </DetailRow>
        </DetailList>
      </section>
      <section aria-labelledby="settings-h" className="mb-6 max-w-3xl">
        <h2 id="settings-h" className="mb-2 font-display text-section font-semibold">Settings and scope</h2>
        {admin ? <SettingsForm key={c.id} connector={c} onSaved={(text) => { setNotice(text); refresh(); }} />
          : (
            <div className="rounded-lg border bg-card">
              <DetailList>
                <DetailRow label="Runs">{c.schedule === "daily" ? "Daily, after 02:00 organization time" : "Only when someone runs it"}</DetailRow>
                <DetailRow label="Candidates go to">{c.reviewer?.name ?? "The IT coordinator"}</DetailRow>
              </DetailList>
              <div className="border-t"><ScopeSummary source={c.source} scope={c.scope} /></div>
              <p className="border-t px-4 py-2.5 text-sm text-muted-foreground">Only Admins change a connector's settings, check its access and run it.</p>
            </div>
          )}
      </section>
      <section aria-labelledby="access-h" className="mb-6 max-w-4xl">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <h2 id="access-h" className="font-display text-section font-semibold">Access</h2>
          {admin && c.available && <Button size="sm" variant="outline" className="ml-auto" onClick={() => void checkAccess()} disabled={checking}>{checking ? "Checking…" : "Check access again"}</Button>}
        </div>
        <p className="mb-3 max-w-prose text-sm text-muted-foreground">
          What this source reads with. How to grant each: <Link className="underline" to={docLink(info?.doc ?? "")}>permissions reference</Link>.
        </p>
        <div className="mb-3"><PermissionsTable permissions={c.permissions} caption={`Permissions ${c.source_label} uses`} /></div>
        <h3 className="mb-1.5 font-semibold">Last access check</h3>
        {c.access_check ? <AccessCheckResult check={c.access_check} /> : <p className="text-muted-foreground">Not checked yet.{admin ? " Check access to see which permissions work." : ""}</p>}
      </section>
      <section aria-labelledby="runs-h" className="mb-6">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <h2 id="runs-h" className="font-display text-section font-semibold">Recent runs</h2>
          <Link to={`/discovery/runs?connector=${c.id}`} className="ml-auto text-sm">All runs of {c.name}</Link>
        </div>
        {runs.isError && <ErrorBanner message={problemText(runs.error, "Couldn't load the runs.")} onRetry={() => void runs.refetch()} />}
        {!runs.data && !runs.isError && <Loading what="runs" rows={2} />}
        {runs.data && runs.data.length === 0 && <p className="text-muted-foreground">No runs yet.{admin ? " Use Run now above." : ""}</p>}
        {runs.data && runs.data.length > 0 && <RecentRuns runs={runs.data.slice(0, 10)} />}
      </section>
      {admin && (
        <section aria-labelledby="delete-h" className="max-w-3xl rounded-lg border bg-card px-4 py-3">
          <h2 id="delete-h" className="font-display text-section font-semibold">Delete this connector</h2>
          <p className="mb-2 text-sm text-muted-foreground">Its runs and what it saw in the source are removed. Records created or linked from it stay as they are, and its connection stays for other connectors.</p>
          <Button variant="outline" onClick={() => setDeleting(true)}>Delete connector</Button>
        </section>
      )}
      <DeleteConnectorDialog connector={deleting ? c : null} onClose={() => setDeleting(false)} />
      <AcceptEmptyDialog connector={accepting ? c : null} onClose={() => setAccepting(false)} onRun={async () => { await runNow(true); setAccepting(false); }} />
    </>
  );
}

function RecentRuns({ runs }: { runs: ConnectorRun[] }) {
  return (
    <TableRegion label="Recent runs table">
      <Table>
        <TableCaption className="sr-only">The last ten runs, newest first</TableCaption>
        <TableHeader><TableRow>
          <TableHead scope="col">Queued</TableHead><TableHead scope="col">Status</TableHead><TableHead scope="col">Trigger</TableHead>
          <TableHead scope="col">Duration</TableHead><TableHead scope="col">Seen</TableHead><TableHead scope="col">Changes</TableHead>
        </TableRow></TableHeader>
        <TableBody>
          {runs.map((r) => (
            <TableRow key={r.id} className="align-top">
              <TableHead scope="row" className="font-normal text-foreground"><Link to={`/discovery/runs/${r.id}`} className="tabular">{when(r.queued_at ?? r.started_at)}</Link></TableHead>
              <TableCell className="whitespace-normal"><RunStatusChip status={r.status} />{r.error && <span className="mt-1 block max-w-sm text-sm text-error-foreground">{r.error}</span>}</TableCell>
              <TableCell className="whitespace-normal">{TRIGGER_LABEL[r.trigger]}{r.requested_by && <span className="block text-sm text-muted-foreground">by {r.requested_by.name}</span>}</TableCell>
              <TableCell className="tabular">{durationText(r.duration_seconds)}</TableCell>
              <TableCell className="tabular">{seenKept(r)}</TableCell>
              <TableCell className="tabular">{changesText(r)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableRegion>
  );
}

/** What the settings form edits, to tell a change someone else saved from a run's bookkeeping. */
const settingsOf = (x: Connector) => JSON.stringify([x.name, x.connection?.id ?? null, x.enabled, x.schedule, x.reviewer?.id ?? null, x.scope]);

function SettingsForm({ connector: c, onSaved }: { connector: Connector; onSaved: (text: string) => void }) {
  const kind = sourceInfo(c.source)?.connection ?? "microsoft";
  const attr = `${kind}_connection`;
  const connections = useApiQuery<Connection[]>(connectionsPath(kind));
  const stewards = useApiQuery<UserRef[]>("stewards");
  const [name, setName] = useState(c.name);
  const [connection, setConnection] = useState(c.connection?.id ?? "");
  const [enabled, setEnabled] = useState(c.enabled);
  const [schedule, setSchedule] = useState(c.schedule);
  const [reviewer, setReviewer] = useState(c.reviewer ? String(c.reviewer.id) : "");
  const [scope, setScope] = useState<ScopeValues>(() => scopeValues(c.source, c.scope));
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [saving, setSaving] = useState(false);
  // The settings the form started from (the page's read, or the latest version loaded after a 412).
  const [base, setBase] = useState(c);
  const [stale, setStale] = useState<Connector | null>(null);
  function loadLatest(latest: Connector) {
    setBase(latest);
    setName(latest.name); setConnection(latest.connection?.id ?? ""); setEnabled(latest.enabled); setSchedule(latest.schedule);
    setReviewer(latest.reviewer ? String(latest.reviewer.id) : ""); setScope(scopeValues(latest.source, latest.scope));
    setErrors({});
    setStale(null);
  }
  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (saving) return;
    const body: Record<string, unknown> = { scope: scopeBody(c.source, scope) };
    if (name.trim() !== base.name) body.name = name.trim();
    if (connection && connection !== base.connection?.id) body[attr] = connection;
    if (enabled !== base.enabled) body.enabled = enabled;
    if (schedule !== base.schedule) body.schedule = schedule;
    if (reviewer !== (base.reviewer ? String(base.reviewer.id) : "")) body.reviewer = reviewer ? Number(reviewer) : null;
    setSaving(true);
    setErrors({});
    try {
      // If-Match: runs change the connector's revision while this form stays open, so the ETag is read now and
      // sent only while the settings are still the ones the form started from; otherwise someone else changed them and
      // the save offers the latest version instead of overwriting theirs.
      const now = await apiWithEtag<Connector>(`connectors/${c.id}`);
      if (settingsOf(now.data) !== settingsOf(base)) return setStale(now.data);
      setBase(await api<Connector>(`connectors/${c.id}`, { method: "PATCH", body, headers: ifMatch(now.etag) }));
      onSaved("Settings saved. The next run uses them.");
    } catch (err) {
      if (isStaleWrite(err)) setStale((await apiWithEtag<Connector>(`connectors/${c.id}`).catch(() => null))?.data ?? base);
      else setErrors(err instanceof Problem && Object.keys(err.errors).length
        ? Object.fromEntries(Object.entries(scopeErrors(c.source, err.errors)).map(([k, v]) => [k === "name" ? "connector-name" : k === attr ? "connector-connection" : k, v]))
        : { "connector-name": [problemText(err, "Couldn't save the settings.")] });
    } finally {
      setSaving(false);
    }
  }
  return (
    <form onSubmit={save} noValidate className="rounded-lg border bg-card px-4 py-4">
      {stale && <StaleBanner what="connector" onLoad={() => loadLatest(stale)} />}
      <ErrorSummary errors={errors} focusOnShow="field" />
      <Field id="connector-name" label="Name (required)" errors={errors}>
        <Input id="connector-name" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field id="connector-connection" label={CONNECTION_LABEL[kind]} errors={errors} help={<>Connectors run only on a tested connection. Add or test one in <Link className="underline" to={connectionsLink(kind)}>Connections</Link>.</>}>
        <Select id="connector-connection" value={connection} onChange={(e) => setConnection(e.target.value)} className="w-96 max-w-full">
          {!connection && <option value="">Choose a connection</option>}
          {c.connection && !(connections.data ?? []).some((x) => x.id === c.connection!.id) && <option value={c.connection.id}>{c.connection.name}</option>}
          {(connections.data ?? []).map((x) => <option key={x.id} value={x.id}>{x.name}{x.test_current ? "" : " (not tested since its last change)"}</option>)}
        </Select>
      </Field>
      <label className="mb-3 flex items-center gap-2 text-sm">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} className="h-4 w-4 accent-primary" />
        Turned on (runs on its schedule and with Run now)
      </label>
      <Field id="connector-schedule" label="Run" errors={errors}>
        <Select id="connector-schedule" value={schedule} onChange={(e) => setSchedule(e.target.value as Connector["schedule"])} className="w-80 max-w-full">
          <option value="manual">Only when someone runs it</option><option value="daily">Daily, after 02:00 organization time</option>
        </Select>
      </Field>
      <Field id="connector-reviewer" label="Candidates go to" errors={errors} help="Shown in their Needs attention and digest. Nobody chosen: the IT coordinator.">
        <Select id="connector-reviewer" value={reviewer} onChange={(e) => setReviewer(e.target.value)} className="w-80 max-w-full">
          <option value="">The IT coordinator</option>
          {(stewards.data ?? []).map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
        </Select>
      </Field>
      <fieldset className="mb-2 border-t pt-4">
        <legend className="mb-2 font-display text-section font-semibold">Scope</legend>
        <ScopeFields source={c.source} values={scope} onChange={setScope} errors={errors} />
      </fieldset>
      <div className="flex flex-wrap gap-2 border-t pt-4">
        <Button type="submit" disabled={saving}>{saving ? "Saving…" : "Save settings"}</Button>
      </div>
    </form>
  );
}

/** The empty-listing guard's way through (POST connectors/{id}/run {accept_empty_listing: true}): a checkbox
 *  confirmation, then a destructive button that names the consequence. Once queued the opener is gone, so focus falls
 *  back to the page's heading. */
function AcceptEmptyDialog({ connector, onClose, onRun }: { connector: Connector | null; onClose: () => void; onRun: () => Promise<void> }) {
  const open = connector !== null;
  const returnFocus = useReturnFocus(open);
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { if (open) { setChecked(false); setError(""); } }, [open]);
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Run “{connector?.name}” once, accepting an empty listing?</DialogTitle>
          <DialogDescription>For this one run, a listing that comes back empty is taken as true: what the connector found earlier and no longer sees is marked “no longer seen” on the records it is linked to, and its discovered credentials are removed. Curated record fields don't change. Later runs are checked again.</DialogDescription>
        </DialogHeader>
        {error && <p className="text-sm text-error-foreground" role="alert">{error}</p>}
        <label className="flex min-h-8 items-center gap-2 text-sm">
          <input type="checkbox" className="h-4 w-4 accent-primary" checked={checked} onChange={(e) => { setChecked(e.target.checked); setError(""); }} />
          I checked in the source that these were deleted
        </label>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="button" variant="destructive"
            aria-busy={busy || undefined} onClick={async () => { if (busy) return; if (!checked) return setError("Tick the box to confirm you checked the source first."); setBusy(true); await onRun(); setBusy(false); }}>{busy ? "Queueing…" : "Run and accept an empty listing"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DeleteConnectorDialog({ connector, onClose }: { connector: Connector | null; onClose: () => void }) {
  const open = connector !== null;
  const returnFocus = useReturnFocus(open);
  const navigate = useNavigate();
  const invalidate = useInvalidate();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // Read with its ETag as the confirmation opens; the delete sends it as If-Match.
  const opened = useOpenedRecord(connector ? `connectors/${connector.id}` : null);
  async function remove() {
    if (!connector) return;
    setBusy(true);
    try {
      await api(`connectors/${connector.id}`, { method: "DELETE", headers: ifMatch(await opened.etag()) });
      void invalidate("connectors");
      navigate("/discovery");
    } catch (e) {
      setError(isStaleWrite(e) ? staleDeleteText("connector") : problemText(e, "Couldn't delete the connector."));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) { setError(""); onClose(); } }}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Delete {connector?.name}?</DialogTitle>
          <DialogDescription>Its runs and source observations are removed. Records created or linked from it stay as they are.</DialogDescription>
        </DialogHeader>
        {error && <p className="text-sm text-error-foreground" role="alert">{error}</p>}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={busy} onClick={() => void remove()}>{busy ? "Deleting…" : `Delete ${connector?.name ?? ""}`}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

