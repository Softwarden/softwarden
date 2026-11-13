/** Administration › Notification channels: the email transport (Microsoft Graph sendMail, an SMTP
 *  server, or the installation's default) with a connection test and a test email, and the Teams channels (Workflows
 *  webhooks) and generic webhooks rules post to, each with a test send. Secrets are write-only: the page shows
 *  whether one is set and since when, and a field to replace it, never the value. Admin only. Loaded on first use. */
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, apiWithEtag, PRECONDITION_FAILED, Problem } from "@/api";
import {
  AdminOnly, EmptyState, ErrorBanner, ErrorSummary, Field, Loading, PageHead, Select, TabStrip, TableRegion, useReturnFocus,
} from "@/components/app";
import { chip, CHIP_WRAP, DetailList, DetailRow, SuccessNote } from "@/components/sw";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { apiKey, problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { OpenedEdit, type OpenedWrite } from "@/components/opened-edit";
import { ifMatch, isStaleWrite, staleDeleteText, useOpenedRecord } from "@/lib/if-match";
import { useMe } from "@/lib/session";
import {
  LastTest, OnOffChip, SecretState, SECURITY_LABEL, TestResultList, TRANSPORT_LABEL,
  type Destination, type EmailSettings, type EmailTransport, type SmtpSecurity, type TestResult, type TestSendResult,
} from "@/features/notify/notify-ui";
import { useResetOn } from "@/features/notify/notify";

const BASE = "notification-settings";
type Tab = "email" | "teams" | "webhooks";
const TABS: { id: Tab; label: string }[] = [{ id: "email", label: "Email" }, { id: "teams", label: "Teams channels" }, { id: "webhooks", label: "Webhooks" }];

export function NotificationChannelsPage() {
  return (
    <AdminOnly title="Notification channels" what="how Softwarden sends notices: the email transport and its secrets, Teams channels and webhooks">
      <Channels />
    </AdminOnly>
  );
}

function Channels() {
  const [params, setParams] = useSearchParams();
  const asked = params.get("tab") as Tab | null;
  const tab: Tab = asked && TABS.some((t) => t.id === asked) ? asked : "email";
  return (
    <>
      <PageHead title="Notification channels" desc="Where notices go: email, Teams channels and webhooks. Secrets are stored encrypted and never shown again; replace one by typing a new value." />
      <p className="mb-4 max-w-prose text-sm text-muted-foreground">
        Choose which reminders use which channel in <Link className="underline" to="/admin/notification-rules">Notification rules</Link>. Every message, test sends included, is in the <Link className="underline" to="/admin/notification-log">Notification log</Link>.
      </p>
      <TabStrip tabs={TABS} tab={tab} label="Channels" onSelect={(t) => setParams(t === "email" ? {} : { tab: t })} />
      <div role="tabpanel" id="tabpanel" aria-labelledby={`tab-${tab}`}>
        {tab === "email" && <EmailPanel />}
        {tab === "teams" && <DestinationsPanel kind="teams" />}
        {tab === "webhooks" && <DestinationsPanel kind="webhooks" />}
      </div>
    </>
  );
}

// ---- email ----------------------------------------------------------------------------------------------------------

const EMAIL = `${BASE}/email`;

/** The status, the form and the tests. The form starts from the settings as loaded and starts again only after a
 *  save, a forget or "Load the latest version" (`version`), so a test (which records its outcome) never drops what is
 *  being typed, and a test's result stays on screen. Tests use the saved settings, so they wait while a save is on its
 *  way (a test then ran on the previous secret), and a save waits for a test and its reload (the test changes the
 *  settings' version, so a save sent before the reload was refused as someone else's change). A save clears the last
 *  "saved" note, so the next one is said again. */
function EmailPanel() {
  const q = useQuery({ queryKey: [...apiKey(EMAIL), "etag"], queryFn: () => apiWithEtag<EmailSettings>(EMAIL), gcTime: 0 });
  const [version, setVersion] = useState(0);
  const [notice, setNotice] = useState("");
  const [forgetting, setForgetting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  if (q.isError) return <ErrorBanner message={problemText(q.error, "Couldn't load the email settings.")} onRetry={() => void q.refetch()} />;
  if (!q.data) return <Loading what="the email settings" />;
  const restart = async (text: string) => { await q.refetch(); setVersion((v) => v + 1); setNotice(text); };
  const s = q.data.data;
  return (
    <>
      <EmailStatus s={s} />
      <SuccessNote text={notice} />
      <EmailForm key={version} settings={s} etag={q.data.etag} onSaved={(text) => void restart(text)} onNothing={() => setNotice("Nothing changed.")}
        onForget={() => setForgetting(true)} blocked={testing} onSaving={(on) => { setSaving(on); if (on) setNotice(""); }} />
      <EmailTest blocked={saving} onBusy={setTesting} onTested={() => q.refetch()} />
      <ForgetDialog open={forgetting} etag={q.data.etag} onClose={() => setForgetting(false)}
        onDone={() => { setForgetting(false); void restart("Email settings forgotten. Email goes through the installation's default."); }} />
    </>
  );
}

type EmailFormState = {
  transport: EmailTransport; graph_tenant_id: string; graph_client_id: string; graph_secret: string; sender_mailbox: string;
  smtp_host: string; smtp_port: string; smtp_security: SmtpSecurity; smtp_username: string; smtp_password: string; remove_password: boolean; from_address: string;
};
const PLAIN = ["transport", "graph_tenant_id", "graph_client_id", "sender_mailbox", "smtp_host", "smtp_security", "smtp_username", "from_address"] as const;

function EmailStatus({ s }: { s: EmailSettings }) {
  const fallbackOff = s.transport === "installation_default" && !s.installation_default.configured;
  return (
    <section aria-labelledby="email-status" className="mb-4 max-w-3xl rounded-lg border bg-card">
      <h2 id="email-status" className="border-b px-4 py-2.5 font-display text-section font-semibold">Status</h2>
      <DetailList>
        <DetailRow label="Sends through">
          {TRANSPORT_LABEL[s.transport]}
          {!s.saved && <span className="block text-sm text-muted-foreground">Not saved yet: the installation's default applies.</span>}
        </DetailRow>
        {s.transport === "installation_default" && (
          <DetailRow label="Installation default">
            {s.installation_default.configured
              ? <span className={cn(chip({ state: "confirmed" }), CHIP_WRAP)}><span aria-hidden="true">✓</span>Configured, from {s.installation_default.from_address}</span>
              : <span className={cn(chip({ state: "gap" }), CHIP_WRAP)}><span aria-hidden="true">⚠</span>No server SMTP settings</span>}
          </DetailRow>
        )}
        <DetailRow label="Last test"><LastTest at={s.last_test_at} ok={s.last_test_ok} detail={s.last_test_detail} /></DetailRow>
      </DetailList>
      {fallbackOff && (
        <p className="border-t px-4 py-2.5 text-sm"><span aria-hidden="true">! </span>No email can be sent until you choose Microsoft Graph or an SMTP server here, or the server's operator sets SOFTWARDEN_SMTP_HOST. Every item stays in Needs attention.</p>
      )}
    </section>
  );
}

const SMTP_PORT: Record<SmtpSecurity, number> = { starttls: 587, tls: 465, none: 25 };
/** Where the SMTP password would go: host (lowercased, no trailing dot), the port sending uses, and the security. */
function smtpDestination(f: { smtp_host: string; smtp_port: string; smtp_security: SmtpSecurity }) {
  const host = f.smtp_host.trim().toLowerCase().replace(/\.$/, "");
  return host ? { host, port: Number(f.smtp_port.trim()) || SMTP_PORT[f.smtp_security], security: f.smtp_security } : null;
}
/** Why a change needs the stored SMTP password entered again, or "" (the API's rule, notify/channel_services). */
function smtpMoved(before: Parameters<typeof smtpDestination>[0], after: Parameters<typeof smtpDestination>[0]) {
  const a = smtpDestination(before);
  const b = smtpDestination(after);
  if (!b) return "";
  if (!a || a.host !== b.host || a.port !== b.port) return "The SMTP server changed, so the stored password isn't sent there: enter it again for the new server, or remove it.";
  if (b.security === "none" && a.security !== "none") return "Without TLS the password is sent in clear: enter it again to confirm, or remove it.";
  return "";
}

function EmailForm({ settings: s, etag, onSaved, onNothing, onForget, blocked, onSaving }: {
  settings: EmailSettings; etag: string | null; onSaved: (text: string) => void; onNothing: () => void; onForget: () => void;
  blocked: boolean; onSaving: (on: boolean) => void;
}) {
  const [initial] = useState<EmailFormState>(() => ({
    transport: s.transport, graph_tenant_id: s.graph_tenant_id, graph_client_id: s.graph_client_id, graph_secret: "", sender_mailbox: s.sender_mailbox,
    smtp_host: s.smtp_host, smtp_port: s.smtp_port === null ? "" : String(s.smtp_port), smtp_security: s.smtp_security, smtp_username: s.smtp_username,
    smtp_password: "", remove_password: false, from_address: s.from_address,
  }));
  const [form, setForm] = useState(initial);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [stale, setStale] = useState(false);
  const [saving, setSaving] = useState(false);
  const invalidate = useInvalidate();
  const text = (k: keyof EmailFormState) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm((f) => ({ ...f, [k]: e.target.value }));
  // The stored SMTP password goes only to the server it was entered for. Another host or
  // port, or security turned off, needs it entered again (or removed) in the same save; the field says why.
  const moved = s.smtp_password_set ? smtpMoved(initial, form) : "";

  async function save(e: React.FormEvent) {
    e.preventDefault();
    const body: Record<string, unknown> = {};
    // The SMTP fields are sent only while SMTP is the chosen transport. Edited and then left for Graph or the
    // installation's default, they stay as stored: the API checks a stored password against a moved server whatever
    // the transport (notify/channel_services), and the field that would explain it isn't on screen then.
    const smtp = form.transport === "smtp";
    for (const k of PLAIN) if (form[k] !== initial[k] && (smtp || !k.startsWith("smtp_"))) body[k] = form[k];
    if (smtp && form.smtp_port !== initial.smtp_port) {
      if (form.smtp_port.trim() && !/^\d+$/.test(form.smtp_port.trim())) return setErrors({ smtp_port: ["Enter a port number such as 587, or leave it empty."] });
      body.smtp_port = form.smtp_port.trim() ? Number(form.smtp_port.trim()) : null;
    }
    if (form.graph_secret) body.graph_secret = form.graph_secret;
    if (smtp) {
      if (form.smtp_password) body.smtp_password = form.smtp_password;
      else if (form.remove_password) body.smtp_password = null;
      else if (moved) return setErrors({ smtp_password: [moved] });
    }
    if (!Object.keys(body).length && s.saved) { setErrors({}); return onNothing(); }
    setSaving(true);
    onSaving(true);
    try {
      await api(EMAIL, { method: "PATCH", body, headers: etag ? { "If-Match": etag } : {} });
      setErrors({});
      void invalidate("organization");
      onSaved("Email settings saved. Send a test email to check them.");
    } catch (err) {
      if (err instanceof Problem && err.status === 412 && err.type === PRECONDITION_FAILED) setStale(true);
      else setErrors(err instanceof Problem && Object.keys(err.errors).length ? err.errors : { transport: [problemText(err, "Couldn't save the email settings.")] });
    } finally {
      setSaving(false);
      onSaving(false);
    }
  }
  const t = form.transport;
  return (
    <>
      {stale && (
        <div className="mb-4 max-w-3xl rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground" role="alert">
          <p className="font-semibold">Someone changed the email settings since you opened them. Nothing was saved.</p>
          <div className="mt-2"><Button size="sm" variant="outline" onClick={() => onSaved("Loaded the latest email settings.")}>Load the latest version</Button></div>
        </div>
      )}
      <Card className="max-w-3xl"><CardContent className="pt-6">
        <form onSubmit={save} noValidate>
          <ErrorSummary errors={errors} focusOnShow="field" />
          <fieldset className="mb-3" aria-describedby="transport-help">
            <legend className="mb-1.5 font-display text-section font-semibold">Send email through</legend>
            {(Object.keys(TRANSPORT_LABEL) as EmailTransport[]).map((v, i) => (
              <label key={v} className="flex min-h-8 items-center gap-2 text-sm">
                <input type="radio" name="transport" id={i === 0 ? "transport" : `transport-${v}`} value={v} checked={t === v}
                  onChange={() => setForm((f) => ({ ...f, transport: v }))} className="h-4 w-4 accent-primary" />
                {TRANSPORT_LABEL[v]}
              </label>
            ))}
            <p id="transport-help" className="text-sm text-muted-foreground">
              For Microsoft 365 use Graph: SMTP sign-in with a password is being retired in Exchange Online. Grant the app Mail.Send through Exchange RBAC for Applications scoped to the sender mailbox, never as a tenant-wide permission.
            </p>
            {errors.transport && <p className="text-sm text-error-foreground">{errors.transport.join(" ")}</p>}
          </fieldset>
          {t === "graph" && (
            <fieldset className="mb-2 border-t pt-4">
              <legend className="mb-3 font-display text-section font-semibold">Microsoft Graph</legend>
              <Field id="graph_tenant_id" label="Tenant ID" errors={errors} help="The directory (tenant) ID or a tenant domain.">
                <Input id="graph_tenant_id" value={form.graph_tenant_id} onChange={text("graph_tenant_id")} autoComplete="off" spellCheck={false} />
              </Field>
              <Field id="graph_client_id" label="Application (client) ID" errors={errors}>
                <Input id="graph_client_id" value={form.graph_client_id} onChange={text("graph_client_id")} autoComplete="off" spellCheck={false} />
              </Field>
              <div className="mb-1.5"><SecretState setOn={s.graph_secret_set ? s.graph_secret_set_on ?? "an earlier date" : null} what="Client secret" /></div>
              <Field id="graph_secret" label={s.graph_secret_set ? "Replace client secret" : "Client secret"} errors={errors}
                help={s.graph_secret_set ? "Leave empty to keep the stored secret. It is never shown." : "Stored encrypted and never shown again."}>
                <Input id="graph_secret" type="password" value={form.graph_secret} onChange={text("graph_secret")} autoComplete="new-password" />
              </Field>
              <Field id="sender_mailbox" label="Sender mailbox" errors={errors} help="The mailbox Graph sends as, such as softwarden@contoso.com.">
                <Input id="sender_mailbox" type="email" value={form.sender_mailbox} onChange={text("sender_mailbox")} autoComplete="off" />
              </Field>
            </fieldset>
          )}
          {t === "smtp" && (
            <fieldset className="mb-2 border-t pt-4">
              <legend className="mb-3 font-display text-section font-semibold">SMTP server</legend>
              <Field id="smtp_host" label="Host" errors={errors} help="A host name or address, without https:// or a port.">
                <Input id="smtp_host" value={form.smtp_host} onChange={text("smtp_host")} autoComplete="off" spellCheck={false} />
              </Field>
              <Field id="smtp_security" label="Security" errors={errors}>
                <Select id="smtp_security" value={form.smtp_security} onChange={text("smtp_security")}>
                  {(Object.keys(SECURITY_LABEL) as SmtpSecurity[]).map((v) => <option key={v} value={v}>{SECURITY_LABEL[v]}</option>)}
                </Select>
              </Field>
              <Field id="smtp_port" label="Port" errors={errors} help="Leave empty for the usual port: 587 for STARTTLS, 465 for TLS, 25 for none.">
                <Input id="smtp_port" inputMode="numeric" value={form.smtp_port} onChange={text("smtp_port")} className="w-32" />
              </Field>
              <Field id="smtp_username" label="User name" errors={errors} help="Leave empty for a relay that doesn't sign in.">
                <Input id="smtp_username" value={form.smtp_username} onChange={text("smtp_username")} autoComplete="off" />
              </Field>
              <div className="mb-1.5"><SecretState setOn={s.smtp_password_set ? s.smtp_password_set_on ?? "an earlier date" : null} what="Password" /></div>
              <Field id="smtp_password" label={!s.smtp_password_set ? "Password" : moved ? "New password (required, or remove it)" : "Replace password"} errors={errors}
                help={!s.smtp_password_set ? "Stored encrypted and never shown again." : moved || "Leave empty to keep the stored password. It is never shown."}>
                <Input id="smtp_password" type="password" value={form.smtp_password} onChange={text("smtp_password")} autoComplete="new-password" />
              </Field>
              {s.smtp_password_set && (
                <label className="mb-3 flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={form.remove_password} onChange={(e) => setForm((f) => ({ ...f, remove_password: e.target.checked }))} className="h-4 w-4 accent-primary" />
                  Remove the stored password (no sign-in)
                </label>
              )}
            </fieldset>
          )}
          {t !== "installation_default" && (
            <Field id="from_address" label="From address" errors={errors} help={t === "graph" ? "Leave empty to send from the sender mailbox." : "The address messages come from."}>
              <Input id="from_address" type="email" value={form.from_address} onChange={text("from_address")} autoComplete="off" />
            </Field>
          )}
          <div className="flex flex-wrap items-center gap-2 border-t pt-4">
            <Button type="submit" disabled={saving || blocked}>{saving ? "Saving…" : "Save email settings"}</Button>
            {s.saved && <Button type="button" variant="outline" onClick={onForget}>Forget settings</Button>}
          </div>
        </form>
      </CardContent></Card>
    </>
  );
}

/** Test connection and Send test email, on the saved settings; the outcome is shown here and announced. */
function EmailTest({ blocked, onBusy, onTested }: { blocked: boolean; onBusy: (on: boolean) => void; onTested: () => Promise<unknown> }) {
  const me = useMe();
  const [testing, setTesting] = useState<"" | "connection" | "send">("");
  const [connection, setConnection] = useState<TestResult | null>(null);
  const [sent, setSent] = useState<TestSendResult | null>(null);
  const [testError, setTestError] = useState("");
  const invalidate = useInvalidate();
  async function test(which: "connection" | "send") {
    setTesting(which);
    onBusy(true);
    setConnection(null);
    setSent(null);
    setTestError("");
    try {
      if (which === "connection") setConnection(await api<TestResult>(`${EMAIL}/test-connection`, { method: "POST" }));
      else { setSent(await api<TestSendResult>(`${EMAIL}/test-send`, { method: "POST" })); void invalidate("notification-log"); }
      await onTested();
    } catch (err) {
      setTestError(err instanceof Problem && Object.keys(err.errors).length ? Object.values(err.errors).flat().join(" ") : problemText(err, "Couldn't run the test."));
    } finally {
      setTesting("");
      onBusy(false);
    }
  }
  return (
    <section aria-labelledby="email-test" className="mt-4 max-w-3xl rounded-lg border bg-card px-4 py-4">
      <h2 id="email-test" className="mb-1 font-display text-section font-semibold">Test</h2>
      <p className="mb-3 text-sm text-muted-foreground">Tests use the saved settings. The test email goes to you ({me.user.email}) and is recorded in the notification log.</p>
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" disabled={!!testing || blocked} onClick={() => void test("connection")}>{testing === "connection" ? "Checking…" : "Test connection"}</Button>
        <Button type="button" variant="outline" disabled={!!testing || blocked} onClick={() => void test("send")}>{testing === "send" ? "Sending…" : "Send test email"}</Button>
      </div>
      <div className="mt-3" role="status" data-testid="email-test-result">
        {connection && (
          <p className={`rounded-md border px-3 py-2 ${connection.ok ? "border-success-border bg-success-bg text-success-foreground" : "border-l-4 border-error-border bg-error-bg"}`}>
            <span className="font-semibold"><span aria-hidden="true">{connection.ok ? "✓ " : "! "}</span>{connection.ok ? "Connection works." : "Connection failed."}</span> {connection.detail}
          </p>
        )}
        {sent && <TestResultList results={[sent]} />}
        {testError && <p className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2"><span aria-hidden="true">! </span>{testError}</p>}
      </div>
    </section>
  );
}

function ForgetDialog({ open, etag, onClose, onDone }: { open: boolean; etag: string | null; onClose: () => void; onDone: () => void }) {
  const returnFocus = useReturnFocus(open);
  const [error, setError] = useResetOn(open, "");
  const [busy, setBusy] = useState(false);
  async function forget() {
    setBusy(true);
    try {
      await api(EMAIL, { method: "DELETE", headers: etag ? { "If-Match": etag } : {} });
      setError("");
      onDone();
    } catch (e) {
      setError(problemText(e, "Couldn't forget the settings."));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Forget the email settings?</DialogTitle>
          <DialogDescription>Deletes the stored settings and secrets. Email then goes through the installation's default, if the server has one.</DialogDescription>
        </DialogHeader>
        {error && <p className="text-sm text-error-foreground" role="alert">{error}</p>}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={busy} onClick={() => void forget()}>{busy ? "Forgetting…" : "Forget settings and secrets"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---- Teams channels and webhooks -----------------------------------------------------------------------------------

const KIND = {
  teams: {
    path: `${BASE}/teams-destinations`, noun: "Teams channel", plural: "Teams channels", add: "Add Teams channel",
    about: "Each is a channel's Workflows webhook: in Teams, open the channel's Workflows, choose \"Send webhook alerts to a channel\" and copy its address. Office 365 connector addresses were retired and are refused.",
    empty: "Add a channel's Workflows address to post reminder cards to it.",
  },
  webhooks: {
    path: `${BASE}/webhook-destinations`, noun: "Webhook", plural: "Webhooks", add: "Add webhook",
    about: "An HTTPS address that receives each notice as JSON, signed with HMAC SHA-256 in the X-Softwarden-Signature header. Private and local addresses are refused.",
    empty: "Add an HTTPS address to send notices to another system.",
  },
} as const;

function DestinationsPanel({ kind }: { kind: "teams" | "webhooks" }) {
  const k = KIND[kind];
  const q = useApiQuery<Destination[]>(k.path);
  const [editing, setEditing] = useState<Destination | "new" | null>(null);
  const [deleting, setDeleting] = useState<Destination | null>(null);
  const [notice, setNotice] = useState("");
  const [result, setResult] = useState<{ name: string; result?: TestSendResult; error?: string } | null>(null);
  const [busy, setBusy] = useState("");
  const invalidate = useInvalidate();
  const rows = q.data;
  async function testSend(d: Destination) {
    setBusy(d.id);
    setResult(null);
    try {
      const done = await api<TestSendResult>(`${k.path}/${d.id}/test-send`, { method: "POST" });
      setResult({ name: d.name, result: done });
      void invalidate(k.path, "notification-log");
    } catch (e) {
      setResult({ name: d.name, error: problemText(e, "Couldn't send the test.") });
    } finally {
      setBusy("");
    }
  }
  const done = (text: string) => { setEditing(null); setDeleting(null); setNotice(text); void invalidate(k.path, "notification-rules"); };
  return (
    <section aria-labelledby={`${kind}-heading`}>
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id={`${kind}-heading`} className="font-display text-section font-semibold">{k.plural}</h2>
          <p className="max-w-prose text-sm text-muted-foreground">{k.about}</p>
        </div>
        <Button onClick={() => setEditing("new")}>{k.add}</Button>
      </div>
      <SuccessNote text={notice} />
      {q.isError && <ErrorBanner message={problemText(q.error, `Couldn't load the ${k.plural.toLowerCase()}.`)} onRetry={() => void q.refetch()} />}
      {!rows && !q.isError && <Loading what={k.plural.toLowerCase()} rows={2} />}
      {rows && rows.length === 0 && <EmptyState title={`No ${k.plural.toLowerCase()} yet`}>{k.empty} Rules choose which ones they post to.</EmptyState>}
      {rows && rows.length > 0 && (
        <TableRegion label={`${k.plural} table`}>
          <Table>
            <TableCaption className="sr-only">{k.plural}, by name</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Name</TableHead><TableHead scope="col">Status</TableHead><TableHead scope="col">Address</TableHead>
              {kind === "webhooks" && <TableHead scope="col">Signing secret</TableHead>}
              <TableHead scope="col">Last test</TableHead><TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((d) => (
                <TableRow key={d.id} className="align-top">
                  <TableHead scope="row" className="font-semibold text-foreground">{d.name}</TableHead>
                  <TableCell><OnOffChip on={d.enabled} /></TableCell>
                  <TableCell className="whitespace-normal">{d.url_host}<span className="block text-sm text-muted-foreground">Address set on {d.url_set_on ?? "—"}; never shown</span></TableCell>
                  {kind === "webhooks" && <TableCell><SecretState setOn={d.signing_secret_set_on} what="Secret" /></TableCell>}
                  <TableCell className="max-w-xs whitespace-normal"><LastTest at={d.last_test_at} ok={d.last_test_ok} detail={d.last_test_detail} /></TableCell>
                  <TableCell className="text-right">
                    <span className="inline-flex flex-wrap justify-end gap-2">
                      <Button size="sm" variant="outline" disabled={busy === d.id} onClick={() => void testSend(d)}>{busy === d.id ? "Sending…" : "Send test"}<span className="sr-only"> to {d.name}</span></Button>
                      <Button size="sm" variant="outline" onClick={() => setEditing(d)}>Edit<span className="sr-only"> {d.name}</span></Button>
                      <Button size="sm" variant="outline" onClick={() => setDeleting(d)}>Delete<span className="sr-only"> {d.name}</span></Button>
                    </span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      <div className="mt-3" role="status" data-testid={`${kind}-test-result`}>
        {result?.result && <TestResultList results={[result.result]} />}
        {result?.error && <p className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2"><span aria-hidden="true">! </span>Test to {result.name} not sent: {result.error}</p>}
      </div>
      <DestinationDialog kind={kind} item={editing} onClose={() => setEditing(null)} onDone={done} />
      <DeleteDestinationDialog kind={kind} item={deleting} onClose={() => setDeleting(null)} onDone={done} />
    </section>
  );
}

function DestinationDialog({ kind, item, onClose, onDone }: { kind: "teams" | "webhooks"; item: Destination | "new" | null; onClose: () => void; onDone: (text: string) => void }) {
  const k = KIND[kind];
  const open = item !== null;
  const returnFocus = useReturnFocus(open);
  const existing = item && item !== "new" ? item : null;
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>{existing ? `Edit ${k.noun.toLowerCase()} ${existing.name}` : k.add}</DialogTitle>
          <DialogDescription>{existing ? "Leave the address and secret empty to keep the stored ones; they are never shown." : "The address is stored encrypted and never shown again; only its host is."}</DialogDescription>
        </DialogHeader>
        {open && !existing && <DestinationForm key="new" kind={kind} existing={null} onCancel={onClose} onDone={onDone} />}
        {open && existing && (
          <OpenedEdit<Destination> key={existing.id} path={`${k.path}/${existing.id}`} what={k.noun.replace("Webhook", "webhook")}>
            {(fresh, write) => <DestinationForm kind={kind} existing={fresh} write={write} onCancel={onClose} onDone={onDone} />}
          </OpenedEdit>
        )}
      </DialogContent>
    </Dialog>
  );
}

function DestinationForm({ kind, existing, write, onCancel, onDone }: { kind: "teams" | "webhooks"; existing: Destination | null; write?: OpenedWrite; onCancel: () => void; onDone: (text: string) => void }) {
  const k = KIND[kind];
  const [name, setName] = useState(existing?.name ?? "");
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState("");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [saving, setSaving] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const body: Record<string, unknown> = {};
    if (!existing || name !== existing.name) body.name = name.trim();
    if (!existing || enabled !== existing.enabled) body.enabled = enabled;
    if (!existing || url) body.url = url.trim();
    if (kind === "webhooks" && (!existing || secret)) body.signing_secret = secret;
    if (existing && !Object.keys(body).length) return onDone("Nothing changed.");
    setSaving(true);
    try {
      if (existing) await api(`${k.path}/${existing.id}`, { method: "PATCH", body, headers: await write?.ifMatch() });
      else await api(k.path, { method: "POST", body });
      onDone(existing ? `${k.noun} ${name.trim()} saved.` : `${k.noun} ${name.trim()} added. Send a test to check it.`);
    } catch (err) {
      if (isStaleWrite(err) && write) write.onStale();
      else setErrors(err instanceof Problem && Object.keys(err.errors).length ? err.errors : { name: [problemText(err, "Couldn't save it.")] });
    } finally {
      setSaving(false);
    }
  }
  const p = `dest-`;
  const shown = Object.fromEntries(Object.entries(errors).map(([key, v]) => [`${p}${key}`, v]));
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={shown} />
      <Field id={`${p}name`} label="Name (required)" errors={shown} help="Rules and the notification log show this name.">
        <Input id={`${p}name`} value={name} onChange={(e) => setName(e.target.value)} maxLength={100} />
      </Field>
      <label className="mb-3 flex items-center gap-2 text-sm">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} className="h-4 w-4 accent-primary" />
        On (rules post to it)
      </label>
      {existing && <p className="mb-1.5 text-sm"><span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>Address set on {existing.url_set_on ?? "—"} ({existing.url_host})</span></p>}
      <Field id={`${p}url`} label={existing ? "Replace address" : "Address (required)"} errors={shown}
        help={kind === "teams" ? "The Workflows \"Send webhook alerts to a channel\" address, starting https://." : "An https:// address on a public host."}>
        <Input id={`${p}url`} type="password" value={url} onChange={(e) => setUrl(e.target.value)} autoComplete="off" spellCheck={false} />
      </Field>
      {kind === "webhooks" && <>
        {existing && <p className="mb-1.5 text-sm"><SecretState setOn={existing.signing_secret_set_on} what="Secret" /></p>}
        <Field id={`${p}signing_secret`} label={existing ? "Replace signing secret" : "Signing secret (required)"} errors={shown}
          help="16 characters or more. The receiver checks X-Softwarden-Signature with it.">
          <Input id={`${p}signing_secret`} type="password" value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="new-password" />
        </Field>
      </>}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={saving}>{saving ? "Saving…" : existing ? "Save" : k.add}</Button>
      </DialogFooter>
    </form>
  );
}

function DeleteDestinationDialog({ kind, item, onClose, onDone }: { kind: "teams" | "webhooks"; item: Destination | null; onClose: () => void; onDone: (text: string) => void }) {
  const k = KIND[kind];
  const open = item !== null;
  const returnFocus = useReturnFocus(open);
  const [error, setError] = useResetOn(open ? item.id : null, "");
  const [busy, setBusy] = useState(false);
  // Kept while the dialog animates closed, so its title doesn't go blank.
  const [last, setLast] = useState(item);
  if (item && item !== last) setLast(item);
  const shown = item ?? last;
  // Read with its ETag as the confirmation opens; the delete sends it as If-Match.
  const opened = useOpenedRecord(item ? `${k.path}/${item.id}` : null);
  async function remove() {
    if (!item) return;
    setBusy(true);
    try {
      await api(`${k.path}/${item.id}`, { method: "DELETE", headers: ifMatch(await opened.etag()) });
      setError("");
      onDone(`${k.noun} ${item.name} deleted. Its messages stay in the notification log.`);
    } catch (e) {
      setError(isStaleWrite(e) ? staleDeleteText(k.noun.replace("Webhook", "webhook")) : problemText(e, "Couldn't delete it."));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Delete {k.noun.toLowerCase()} {shown?.name}?</DialogTitle>
          <DialogDescription>Rules stop posting to it and its address is deleted. Messages already sent stay in the notification log.</DialogDescription>
        </DialogHeader>
        {error && <p className="text-sm text-error-foreground" role="alert">{error}</p>}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={busy} onClick={() => void remove()}>{busy ? "Deleting…" : `Delete ${shown?.name ?? ""}`}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
