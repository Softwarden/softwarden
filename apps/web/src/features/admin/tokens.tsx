/** Admin: API tokens. A token acts with its owner's role, never higher; the secret is shown
 *  exactly once, in the create response, and never again (only its SHA-256 is stored, and the list has only its
 *  prefix). Tokens are managed from a signed-in session only: the API refuses a token on these routes. Admin only, as
 *  the API is; other roles see who may open the page. Typed from the generated OpenAPI client (src/lib/openapi.ts). */
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { when } from "@/api";
import {
  AdminOnly, attempt, EmptyState, ErrorBanner, ErrorSummary, Field, Loading, PageHead, Select, TableRegion, useReturnFocus,
} from "@/components/app";
import { chip, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { typedApi, useTypedQuery, type BodyOf, type Schemas } from "@/lib/openapi";
import { problemText, useInvalidate } from "@/lib/query";

type Token = Schemas["ApiToken"];
type Created = Schemas["ApiTokenCreated"];
type Member = Schemas["Member"];

const ROLE: Record<string, string> = { admin: "Admin", editor: "Editor", viewer: "Viewer" };
const LIST = "api-tokens";

function isoDate(d: Date) {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
const daysAhead = (n: number) => { const d = new Date(); d.setDate(d.getDate() + n); return isoDate(d); };

type Status = "Active" | "Expired" | "Revoked";
function statusOf(t: Token): Status {
  if (t.revoked_at) return "Revoked";
  return t.expires_on < isoDate(new Date()) ? "Expired" : "Active";
}

function StatusChip({ status }: { status: Status }) {
  if (status === "Active") return <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>Active</span>;
  if (status === "Expired") return <span className={chip({ state: "overdue" })}><span aria-hidden="true">!</span>Expired</span>;
  return <span className={chip({ state: "neutral" })}><span aria-hidden="true">⊘</span>Revoked</span>;
}

function Modal({ title, description, open, onClose, children, keepOpenOnOutside = false }: {
  title: string; description?: string; open: boolean; onClose: () => void; children: React.ReactNode; keepOpenOnOutside?: boolean;
}) {
  const returnFocus = useReturnFocus(open);
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...returnFocus}
        // The new token's secret is shown once: a stray click outside or Escape mustn't throw it away; Done (or the
        // close button) is the deliberate way out.
        onInteractOutside={keepOpenOnOutside ? (e) => e.preventDefault() : undefined}
        onEscapeKeyDown={keepOpenOnOutside ? (e) => e.preventDefault() : undefined}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>
        {open && children}
      </DialogContent>
    </Dialog>
  );
}

export function ApiTokens() {
  return (
    <AdminOnly title="API tokens" what="the tokens scripts and the MCP server use to call the Softwarden API, who owns each and when it was last used">
      <Tokens />
    </AdminOnly>
  );
}

function Tokens() {
  const tokens = useTypedQuery("api-tokens");
  const members = useTypedQuery("users");
  const [creating, setCreating] = useState(false);
  const [secret, setSecret] = useState<{ name: string; prefix: string; value: string } | null>(null);
  const [revoking, setRevoking] = useState<Token | null>(null);
  const [notice, setNotice] = useState("");
  const origin = window.location.origin;
  // The secret leaves memory with the dialog: only the notice with its prefix stays.
  const finish = (done: { name: string; prefix: string }) => {
    setNotice(`${done.name} was created. Only its prefix, ${done.prefix}…, is shown from now on.`);
    setCreating(false);
    setSecret(null);
  };

  if (tokens.isError && !tokens.isFetching) return <><PageHead title="API tokens" /><ErrorBanner message={problemText(tokens.error, "Couldn't load this page.")} onRetry={() => void tokens.refetch()} /></>;
  const memberRows = members.data ?? [];
  const owners = memberRows.filter((m) => m.active);
  const create = <Button onClick={() => setCreating(true)}>Create token</Button>;
  return (
    <>
      <PageHead title="API tokens" desc="Tokens let scripts, tools and the MCP server call the Softwarden API. A token acts with its owner's role and never higher; a read-only token can't change anything. Manage them here, signed in: a token can't create or revoke tokens.">
        {tokens.data && tokens.data.length > 0 && create}
      </PageHead>
      <SuccessNote text={notice} />
      {!tokens.data ? <Loading what="API tokens" rows={3} /> : tokens.data.length === 0 ? (
        <EmptyState title="No API tokens yet" action={create}>Create a token to let a script read your catalog. Start with a read-only token owned by a Viewer.</EmptyState>
      ) : (
        <TableRegion label="API tokens table">
          <Table>
            <TableCaption className="sr-only">API tokens, newest first</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Name</TableHead><TableHead scope="col">Owner and role</TableHead><TableHead scope="col">Prefix</TableHead>
              <TableHead scope="col">Access</TableHead><TableHead scope="col">Created</TableHead><TableHead scope="col">Expires</TableHead>
              <TableHead scope="col">Last used</TableHead><TableHead scope="col">Status</TableHead><TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {tokens.data.map((t) => {
                const status = statusOf(t);
                const role = memberRows.find((m) => m.id === t.user.id)?.role;
                return (
                  <TableRow key={t.id} className="align-top">
                    <TableHead scope="row" className="min-w-40 max-w-sm whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">{t.name}</TableHead>
                    <TableCell>{t.user.name}{role && <span className="block text-xs text-muted-foreground">{ROLE[role] ?? role}</span>}</TableCell>
                    <TableCell className="font-mono text-sm" data-testid="token-prefix">{t.prefix}…</TableCell>
                    <TableCell>{t.read_only ? "Read-only" : "Read and write"}</TableCell>
                    <TableCell className="tabular">{t.created_at.slice(0, 10)}{t.created_by && <span className="block text-xs text-muted-foreground">by {t.created_by.name}</span>}</TableCell>
                    <TableCell className="tabular">{t.expires_on}</TableCell>
                    <TableCell className="tabular">{t.last_used_at ? when(t.last_used_at) : "Never"}</TableCell>
                    <TableCell><StatusChip status={status} /></TableCell>
                    <TableCell className="text-right">
                      {status !== "Revoked" && <Button size="sm" variant="outline" onClick={() => setRevoking(t)}>Revoke<span className="sr-only"> {t.name}</span></Button>}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      <section className="mt-6 max-w-2xl" aria-labelledby="token-usage">
        <h2 id="token-usage" className="mb-1 font-display text-section font-semibold">Using a token</h2>
        <p className="mb-2 text-sm text-muted-foreground">
          Send it as a bearer token in the Authorization header. Every route is in the <Link className="underline" to="/admin/api">API reference</Link>; tokens, read-only tokens and their errors are in the <Link className="underline" to="/admin/api/guide#signing-in">API guide</Link>.
        </p>
        <code tabIndex={0} role="region" aria-label="Example request" className="block overflow-x-auto whitespace-pre rounded-md border bg-muted px-3 py-2 font-mono text-sm">
          {`curl -H "Authorization: Bearer sw_..." ${origin}/api/v1/applications`}
        </code>
      </section>

      {/* One dialog: the form, then the new secret in its place, so closing it returns focus to the button that opened it. */}
      <Modal title={secret ? "Copy your new token" : "Create API token"} open={creating} keepOpenOnOutside={secret !== null}
        onClose={() => { if (secret) finish(secret); else setCreating(false); }}
        description={secret ? "It won't be shown again. Store it in your secrets manager now." : "The token acts with its owner's role, never higher. You'll see the secret once."}>
        {secret
          ? <SecretPanel secret={secret} onDone={() => finish(secret)} />
          : <CreateForm owners={owners} loading={!members.data} loadError={members.isError ? problemText(members.error, "Couldn't load the users.") : ""}
            onCancel={() => setCreating(false)} onDone={(t) => setSecret({ name: t.name, prefix: t.prefix, value: t.secret })} />}
      </Modal>
      <Modal title={`Revoke ${revoking?.name ?? ""}?`} open={revoking !== null} onClose={() => setRevoking(null)}
        description="Anything using this token stops working right away. This can't be undone.">
        {revoking && <RevokeForm token={revoking} onCancel={() => setRevoking(null)}
          onDone={() => { setNotice(`${revoking.name} was revoked.`); setRevoking(null); }} />}
      </Modal>
    </>
  );
}

function CreateForm({ owners, loading, loadError, onCancel, onDone }: {
  owners: Member[]; loading: boolean; loadError: string; onCancel: () => void; onDone: (t: Created) => void;
}) {
  const [form, setForm] = useState({ user: "", name: "", read_only: true, expires_on: daysAhead(90) });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [saving, setSaving] = useState(false);
  const invalidate = useInvalidate();
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const next: Record<string, string[]> = {};
    if (!form.user) next.user = ["Choose an owner."];
    if (!form.name.trim()) next.name = ["Enter a name."];
    if (!form.expires_on) next.expires_on = ["Choose an expiry date."];
    if (Object.keys(next).length) return setErrors(next);
    const body: BodyOf<"api-tokens", "post"> = { user: Number(form.user), name: form.name.trim(), read_only: form.read_only, expires_on: form.expires_on };
    const result: { token?: Created } = {};
    setSaving(true);
    const failed = await attempt(async () => { result.token = await typedApi("post", "api-tokens", { body }); });
    setSaving(false);
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { name: [failed.detail] });
    void invalidate(LIST);
    if (result.token) onDone(result.token);
  }
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      {loadError && <ErrorBanner message={loadError} />}
      <Field id="user" label="Owner (required)" errors={errors} help="Only active users. The token gets this person's role at most.">
        <Select id="user" value={form.user} disabled={loading} onChange={(e) => setForm({ ...form, user: e.target.value })}>
          <option value="">{loading ? "Loading users…" : "Choose an owner"}</option>
          {owners.map((u) => <option key={u.id} value={u.id}>{u.name} ({ROLE[u.role] ?? u.role})</option>)}
        </Select>
      </Field>
      <Field id="name" label="Name (required)" errors={errors} help="What it's for, such as Nightly inventory export.">
        <Input id="name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} maxLength={100} />
      </Field>
      <div className="mb-4">
        <label className="flex items-start gap-3">
          <input type="checkbox" id="read_only" className="mt-1 h-4 w-4 accent-primary" checked={form.read_only}
            onChange={(e) => setForm({ ...form, read_only: e.target.checked })} aria-describedby="read_only-help" />
          <span>Read-only</span>
        </label>
        <p id="read_only-help" className="mt-1 text-sm text-muted-foreground">Read-only tokens can't change anything. Turn this off only for scripts that must write; the token then writes with its owner's role.</p>
        {errors.read_only && <p className="mt-1 text-sm text-error-foreground">{errors.read_only.join(" ")}</p>}
      </div>
      <Field id="expires_on" label="Expires on (required)" errors={errors} help="Up to 365 days from today. Default is 90 days.">
        <Input id="expires_on" type="date" className="w-48" min={daysAhead(1)} max={daysAhead(365)} value={form.expires_on}
          onChange={(e) => setForm({ ...form, expires_on: e.target.value })} />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={saving}>{saving ? "Creating…" : "Create token"}</Button>
      </DialogFooter>
    </form>
  );
}

/** The new secret, once: a read-only box to select or copy, and a warning that it can't be shown again. */
function SecretPanel({ secret, onDone }: { secret: { name: string; prefix: string; value: string }; onDone: () => void }) {
  const [status, setStatus] = useState("");
  // The form that had focus is gone: start on the warning, so it is read before anything else.
  const note = useRef<HTMLParagraphElement>(null);
  useEffect(() => { note.current?.focus(); }, []);
  async function copy() {
    try {
      await navigator.clipboard.writeText(secret.value);
      setStatus("Token copied to the clipboard.");
    } catch {
      setStatus("Couldn't copy. Select the token and copy it by hand.");
    }
  }
  return (
    <>
      <p ref={note} tabIndex={-1} data-testid="secret-note" className="mb-2 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-sm text-state-pending-foreground outline-none focus:ring-2 focus:ring-ring" role="note">
        <span aria-hidden="true">◐ </span><span className="font-semibold">Shown only once.</span> Softwarden keeps only a fingerprint of it. If you lose it, revoke this token and create another.
      </p>
      <p className="mb-1 text-sm font-medium">{secret.name}</p>
      <code className="mb-2 block select-all break-all rounded-md border bg-muted px-3 py-2 font-mono text-base" data-testid="token-secret">{secret.value}</code>
      <p className="mb-4 text-sm font-medium" role="status">{status}</p>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={() => void copy()}>Copy</Button>
        <Button type="button" onClick={onDone}>Done</Button>
      </DialogFooter>
    </>
  );
}

function RevokeForm({ token, onCancel, onDone }: { token: Token; onCancel: () => void; onDone: () => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const invalidate = useInvalidate();
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    const failed = await attempt(() => typedApi("post", "api-tokens/{id}/revoke", { path: { id: token.id } }));
    setBusy(false);
    if (failed) return setError(failed.detail);
    void invalidate(LIST);
    onDone();
  }
  return (
    <form onSubmit={submit} noValidate>
      {error && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{error}</p>}
      <p className="mb-4">Revoke <strong>{token.name}</strong> ({token.prefix}…), owned by {token.user.name}?</p>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" variant="destructive" disabled={busy}>Revoke {token.name}</Button>
      </DialogFooter>
    </form>
  );
}
