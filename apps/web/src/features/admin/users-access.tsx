/** Users & roles › Invitations and Pending access. Loaded
 *  when one of the tabs is first opened.
 *
 *  Invitations: every invitation with its status (pending until accepted, revoked or past its expiry), filtered by status
 *  and searched by email or name, both in the URL. Resend makes a new link, shown once in the same panel the invite
 *  dialog uses (the old link stops working); Revoke confirms first.
 *
 *  Pending access: identities that signed in through this organization's single sign-on without an account while
 *  auto-provision is on. Approve picks a role and creates (or links) the account; an email whose account belongs to
 *  another organization is refused with 409 invitation-required, and "Invite instead" opens the invite dialog with the
 *  email filled in. Decline confirms first and deletes the request. */
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Problem, when } from "@/api";
import { EmptyState, ErrorBanner, Loading, Select, TableRegion } from "@/components/app";
import { chip, GapChip, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { inviteLinkDescription, InviteLinkPanel, Modal, ROLE_NAME, type InvitePrefill } from "@/features/admin/admin";
import { typedApi, useTypedQuery, type Schemas } from "@/lib/openapi";
import { problemText, useInvalidate } from "@/lib/query";
import { ifMatch, isStaleWrite, useOpenedRecord } from "@/lib/if-match";

type Invitation = Schemas["Invitation"];
type InvitationStatus = Schemas["InvitationStatusEnum"];
type Pending = Schemas["PendingAccess"];
const INVITATION_REQUIRED = "/problems/invitation-required";

/** A filter kept in the URL beside `tab`; changing one starts again from the first page. */
export function useUrlFilters<K extends string>(keys: readonly K[]) {
  const [params, setParams] = useSearchParams();
  const values = Object.fromEntries(keys.map((k) => [k, params.get(k) ?? ""])) as Record<K | "cursor", string>;
  values.cursor = params.get("cursor") ?? "";
  const set = (changes: Partial<Record<K | "cursor", string>>) => setParams((cur) => {
    const next = new URLSearchParams(cur);
    if (!("cursor" in changes)) next.delete("cursor");
    for (const [k, v] of Object.entries(changes) as [string, string][]) { if (v) next.set(k, v); else next.delete(k); }
    return next;
    // Typing in the search replaces the history entry; a select, Clear or a page pushes one, so Back undoes it.
  }, { replace: Object.keys(changes).length === 1 && "q" in changes });
  return [values, set] as const;
}

/** A search box written to the URL after a short pause, so typing doesn't add a request per key. */
export function SearchBox({ id, value, onChange, placeholder }: { id: string; value: string; onChange: (v: string) => void; placeholder: string }) {
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]);
  useEffect(() => {
    if (text === value) return;
    const t = setTimeout(() => onChange(text.trim()), 300);
    return () => clearTimeout(t);
  }, [text]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>Search</Label>
      <Input id={id} type="search" className="w-64" placeholder={placeholder} value={text} onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") onChange(text.trim()); }} />
    </div>
  );
}

export function Pager({ previous, next, onPage, what }: { previous: string | null; next: string | null; onPage: (cursor: string) => void; what: string }) {
  if (!previous && !next) return null;
  const cursorOf = (url: string) => new URL(url, window.location.origin).searchParams.get("cursor") ?? "";
  return (
    <nav className="mt-3 flex gap-2" aria-label={`${what} pages`}>
      <Button type="button" size="sm" variant="outline" disabled={!previous} onClick={() => previous && onPage(cursorOf(previous))}>Previous</Button>
      <Button type="button" size="sm" variant="outline" disabled={!next} onClick={() => next && onPage(cursorOf(next))}>Next</Button>
    </nav>
  );
}

// ---- invitations --------------------------------------------------------------------------------------------------

const STATUSES: { value: InvitationStatus; label: string }[] = [
  { value: "pending", label: "Pending" }, { value: "expired", label: "Expired" }, { value: "accepted", label: "Accepted" }, { value: "revoked", label: "Revoked" },
];

function InvitationStatusChip({ status }: { status: InvitationStatus }) {
  if (status === "pending") return <span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Pending</span>;
  if (status === "accepted") return <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>Accepted</span>;
  if (status === "expired") return <span className={chip({ state: "unreviewed" })}><span aria-hidden="true">○</span>Expired</span>;
  return <span className={chip({ state: "neutral" })}><span aria-hidden="true">⊘</span>Revoked</span>;
}

function InvitationOutcome({ i }: { i: Invitation }) {
  if (i.status === "accepted") return <>Accepted{i.accepted_by ? ` by ${i.accepted_by.name}` : ""}{i.accepted_at && <> on <span className="tabular">{when(i.accepted_at)}</span></>}</>;
  if (i.status === "revoked") return <>Revoked{i.revoked_at && <> on <span className="tabular">{when(i.revoked_at)}</span></>}</>;
  if (i.status === "expired") return <>Expired on <span className="tabular">{when(i.expires_at)}</span></>;
  return <>Expires on <span className="tabular">{when(i.expires_at)}</span></>;
}

export function InvitationsTab({ onInvite }: { onInvite: () => void }) {
  const [filters, setFilters] = useUrlFilters(["status", "q"] as const);
  const status = STATUSES.some((s) => s.value === filters.status) ? (filters.status as InvitationStatus) : undefined;
  const q = useTypedQuery("invitations", { query: { status, q: filters.q || undefined, cursor: filters.cursor || undefined } });
  const invalidate = useInvalidate();
  const [resent, setResent] = useState<Schemas["InvitationCreated"] | null>(null);
  const [revoking, setRevoking] = useState<Invitation | null>(null);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  async function resend(i: Invitation) {
    if (busy) return;
    setBusy(i.id); setError(""); setNotice("");
    try {
      setResent(await typedApi("post", "invitations/{id}/resend", { path: { id: i.id } }));
      void invalidate("invitations");
    } catch (e) {
      setError(problemText(e, `Couldn't make a new link for ${i.email}.`));
    } finally {
      setBusy("");
    }
  }
  const finishResend = () => {
    if (resent) setNotice(`A new link for ${resent.email} was made${resent.emailed ? " and emailed" : ""}; the previous link no longer works.`);
    setResent(null);
  };
  const rows = q.data?.results;
  const filtered = !!(status || filters.q);
  return (
    <>
      <SuccessNote text={notice} />
      {error && <ErrorBanner message={error} />}
      <div className="mb-3 flex flex-wrap items-end gap-3">
        <SearchBox id="invitations-q" value={filters.q} onChange={(v) => setFilters({ q: v })} placeholder="Email or name" />
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="invitations-status">Status</Label>
          <Select id="invitations-status" value={status ?? ""} onChange={(e) => setFilters({ status: e.target.value })}>
            <option value="">All</option>
            {STATUSES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </Select>
        </div>
        {filtered && <Button type="button" variant="outline" onClick={() => setFilters({ status: "", q: "" })}>Clear filters</Button>}
      </div>
      {q.isError && <ErrorBanner message={problemText(q.error, "Couldn't load the invitations.")} onRetry={() => void q.refetch()} />}
      {!rows && !q.isError && <Loading what="invitations" rows={3} />}
      {rows && <p className="sr-only" role="status">{rows.length} {rows.length === 1 ? "invitation" : "invitations"} shown</p>}
      {rows && rows.length === 0 && (filtered
        ? <EmptyState title="No invitations match these filters" action={<Button type="button" variant="outline" onClick={() => setFilters({ status: "", q: "" })}>Clear filters</Button>}>Change the search or the status to see more.</EmptyState>
        : <EmptyState title="No invitations yet" action={<Button type="button" variant="outline" onClick={onInvite}>Invite user</Button>}>Invite someone by email: they get a link to join, and they're listed here until they accept it.</EmptyState>)}
      {rows && rows.length > 0 && (
        <TableRegion label="Invitations table">
          <Table>
            <TableCaption className="sr-only">Invitations, newest first, with their status</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Email</TableHead><TableHead scope="col">Role</TableHead><TableHead scope="col">Status</TableHead>
              <TableHead scope="col">Invited</TableHead><TableHead scope="col">Outcome</TableHead><TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((i) => (
                <TableRow key={i.id} className="align-top">
                  <TableHead scope="row" className="min-w-40 max-w-sm whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
                    {i.email}{i.name && <span className="block text-xs font-normal text-muted-foreground">{i.name}</span>}
                  </TableHead>
                  <TableCell>{ROLE_NAME[i.role] ?? i.role}</TableCell>
                  <TableCell><InvitationStatusChip status={i.status} /></TableCell>
                  <TableCell className="whitespace-normal">
                    <span className="tabular">{when(i.created_at)}</span>
                    <span className="block text-sm text-muted-foreground">{i.invited_by ? `by ${i.invited_by.name}` : "by someone no longer here"}</span>
                  </TableCell>
                  <TableCell className="whitespace-normal"><InvitationOutcome i={i} /></TableCell>
                  <TableCell className="text-right">
                    <span className="inline-flex flex-wrap justify-end gap-2">
                      {(i.status === "pending" || i.status === "expired") && (
                        // Not disabled while it works: the dialog it opens returns focus to it.
                        <Button size="sm" variant="outline" aria-busy={busy === i.id || undefined} onClick={() => void resend(i)}>{busy === i.id ? "Making a link…" : "Resend"}<span className="sr-only"> the invitation for {i.email}</span></Button>
                      )}
                      {i.status === "pending" && <Button size="sm" variant="outline" onClick={() => setRevoking(i)}>Revoke<span className="sr-only"> the invitation for {i.email}</span></Button>}
                    </span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      {q.data && <Pager previous={q.data.previous} next={q.data.next} onPage={(cursor) => setFilters({ cursor })} what="Invitations" />}
      <Modal title={resent?.emailed ? "New invitation link emailed" : "Copy the new invitation link"} open={resent !== null} keepOpenOnOutside onClose={finishResend}
        description={resent ? inviteLinkDescription(resent, true) : undefined}>
        {resent && <InviteLinkPanel invitation={resent} onDone={finishResend} />}
      </Modal>
      <Modal title={`Revoke the invitation for ${revoking?.email ?? ""}?`} open={revoking !== null} onClose={() => setRevoking(null)}
        description="Its link stops working at once. The invitation stays in this list as revoked; invite them again to send a new one.">
        {revoking && <RevokeForm invitation={revoking} onCancel={() => setRevoking(null)}
          onDone={(text) => { setRevoking(null); setNotice(text); void invalidate("invitations"); }} />}
      </Modal>
    </>
  );
}

function RevokeForm({ invitation, onCancel, onDone }: { invitation: Invitation; onCancel: () => void; onDone: (text: string) => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await typedApi("post", "invitations/{id}/revoke", { path: { id: invitation.id } });
      onDone(`The invitation for ${invitation.email} was revoked; its link no longer works.`);
    } catch (err) {
      setError(problemText(err, "Couldn't revoke the invitation."));
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit} noValidate>
      {error && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{error}</p>}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" variant="destructive" disabled={busy}>{busy ? "Revoking…" : `Revoke the invitation for ${invitation.email}`}</Button>
      </DialogFooter>
    </form>
  );
}

// ---- pending access -----------------------------------------------------------------------------------------------

const PROVIDER_FILTER: { value: "entra" | "okta" | "google" | "other"; label: string }[] = [
  { value: "entra", label: "Entra" }, { value: "okta", label: "Okta" }, { value: "google", label: "Google" }, { value: "other", label: "Other (SAML and other providers)" },
];
type ProviderFilter = (typeof PROVIDER_FILTER)[number]["value"];

export function PendingAccessTab({ onInvite }: { onInvite: (prefill: InvitePrefill) => void }) {
  const [filters, setFilters] = useUrlFilters(["provider", "q"] as const);
  const provider = PROVIDER_FILTER.some((p) => p.value === filters.provider) ? (filters.provider as ProviderFilter) : undefined;
  const q = useTypedQuery("pending-access", { query: { provider, q: filters.q || undefined, cursor: filters.cursor || undefined } });
  const invalidate = useInvalidate();
  const [approving, setApproving] = useState<Pending | null>(null);
  // The row's Approve button: "Invite instead" closes the approve dialog, so the invite dialog returns focus here.
  const approver = useRef<HTMLElement | null>(null);
  const [declining, setDeclining] = useState<Pending | null>(null);
  const [notice, setNotice] = useState("");
  const done = (text: string) => { setApproving(null); setDeclining(null); setNotice(text); void invalidate("pending-access", "users"); };
  const rows = q.data?.results;
  const filtered = !!(provider || filters.q);
  return (
    <>
      <SuccessNote text={notice} />
      <p className="mb-3 max-w-prose text-sm text-muted-foreground">
        People who signed in through this organization's single sign-on without an account wait here while auto-provision is on (<Link className="underline" to="/admin/organization">Organization settings</Link>). They have no access until you approve them with a role.
      </p>
      <div className="mb-3 flex flex-wrap items-end gap-3">
        <SearchBox id="pending-q" value={filters.q} onChange={(v) => setFilters({ q: v })} placeholder="Email or name" />
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="pending-provider">Provider</Label>
          <Select id="pending-provider" value={provider ?? ""} onChange={(e) => setFilters({ provider: e.target.value })}>
            <option value="">All</option>
            {PROVIDER_FILTER.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
          </Select>
        </div>
        {filtered && <Button type="button" variant="outline" onClick={() => setFilters({ provider: "", q: "" })}>Clear filters</Button>}
      </div>
      {q.isError && <ErrorBanner message={problemText(q.error, "Couldn't load pending access.")} onRetry={() => void q.refetch()} />}
      {!rows && !q.isError && <Loading what="pending access" rows={3} />}
      {rows && <p className="sr-only" role="status">{rows.length} {rows.length === 1 ? "request" : "requests"} shown</p>}
      {rows && rows.length === 0 && (filtered
        ? <EmptyState title="No requests match these filters" action={<Button type="button" variant="outline" onClick={() => setFilters({ provider: "", q: "" })}>Clear filters</Button>}>Change the search or the provider to see more.</EmptyState>
        : <EmptyState title="Nobody is waiting for access">With single sign-on active and auto-provision on, someone who signs in without an account is listed here for you to approve or decline. Otherwise invite people by email.</EmptyState>)}
      {rows && rows.length > 0 && (
        <TableRegion label="Pending access table">
          <Table>
            <TableCaption className="sr-only">Pending access requests, most recently seen first</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Email</TableHead><TableHead scope="col">Provider</TableHead><TableHead scope="col">First seen</TableHead>
              <TableHead scope="col">Last seen</TableHead><TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((r) => {
                const who = r.email || r.name || "this request";
                return (
                  <TableRow key={r.id} className="align-top">
                    <TableHead scope="row" className="min-w-40 max-w-sm whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
                      {r.email || <GapChip label="No email from the provider" />}
                      {r.name && r.name !== r.email && <span className="block text-xs font-normal text-muted-foreground">{r.name}</span>}
                    </TableHead>
                    <TableCell className="whitespace-normal">{r.provider}<span className="block text-xs text-muted-foreground [overflow-wrap:anywhere]">{r.issuer}</span></TableCell>
                    <TableCell className="tabular">{when(r.first_seen)}</TableCell>
                    <TableCell className="tabular">{when(r.last_seen)}</TableCell>
                    <TableCell className="text-right">
                      <span className="inline-flex flex-wrap justify-end gap-2">
                        <Button size="sm" variant="outline" onClick={(e) => { approver.current = e.currentTarget; setApproving(r); }}>Approve<span className="sr-only"> {who}</span></Button>
                        <Button size="sm" variant="outline" onClick={() => setDeclining(r)}>Decline<span className="sr-only"> {who}</span></Button>
                      </span>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      {q.data && <Pager previous={q.data.previous} next={q.data.next} onPage={(cursor) => setFilters({ cursor })} what="Pending access" />}
      <Modal title={`Approve ${approving?.email || approving?.name || "this request"}`} open={approving !== null} onClose={() => setApproving(null)}
        description="Choose the role they get. They sign in with single sign-on; no password is set.">
        {approving && <ApproveForm request={approving} onCancel={() => setApproving(null)} onDone={done}
          onInvite={() => { const r = approving; setApproving(null); onInvite({ email: r.email, name: r.name, returnTo: approver.current }); }} />}
      </Modal>
      <Modal title={`Decline ${declining?.email || declining?.name || "this request"}?`} open={declining !== null} onClose={() => setDeclining(null)}
        description="The request is deleted and they get no access. If they sign in again while auto-provision is on, they're listed again.">
        {declining && <DeclineForm request={declining} onCancel={() => setDeclining(null)} onDone={done} />}
      </Modal>
    </>
  );
}

function ApproveForm({ request, onCancel, onDone, onInvite }: { request: Pending; onCancel: () => void; onDone: (text: string) => void; onInvite: () => void }) {
  const [role, setRole] = useState<Schemas["RoleEnum"]>("viewer");
  const [error, setError] = useState("");
  const [needsInvite, setNeedsInvite] = useState(false);
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError(""); setNeedsInvite(false);
    try {
      const member = await typedApi("post", "pending-access/{id}/approve", { path: { id: request.id }, body: { role } });
      onDone(`${member.name || member.email} can sign in now, as ${ROLE_NAME[member.role] ?? member.role}.`);
    } catch (err) {
      setBusy(false);
      if (err instanceof Problem && err.type === INVITATION_REQUIRED) setNeedsInvite(true);
      setError(err instanceof Problem ? (err.errors.role?.join(" ") ?? err.detail) : problemText(err, "Couldn't approve the request."));
    }
  }
  return (
    <form onSubmit={submit} noValidate>
      {error && (
        <div className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">
          <p>{error}</p>
          {needsInvite && <p className="mt-1 text-sm">An invitation lets them accept with the account they already have.</p>}
        </div>
      )}
      <div className="mb-4 flex flex-col gap-1.5">
        <Label htmlFor="approve-role">Role</Label>
        <Select id="approve-role" value={role} onChange={(e) => setRole(e.target.value as Schemas["RoleEnum"])}>
          <option value="viewer">Viewer</option><option value="editor">Editor</option><option value="admin">Admin</option>
        </Select>
        <span className="text-sm text-muted-foreground">Someone already a member here keeps their role; only their sign-in is linked.</span>
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        {needsInvite
          ? <Button type="button" onClick={onInvite}>Invite instead</Button>
          : <Button type="submit" disabled={busy}>{busy ? "Approving…" : "Approve"}</Button>}
      </DialogFooter>
    </form>
  );
}

function DeclineForm({ request, onCancel, onDone }: { request: Pending; onCancel: () => void; onDone: (text: string) => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const who = request.email || request.name;
  // Read with its ETag as the confirmation opens; the decline sends it as If-Match.
  const opened = useOpenedRecord(`pending-access/${request.id}`);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await typedApi("delete", "pending-access/{id}", { path: { id: request.id }, headers: ifMatch(await opened.etag()) });
      onDone(who ? `${who}'s request for access was declined.` : "The request for access was declined.");
    } catch (err) {
      setError(isStaleWrite(err)
        ? "This request changed after you opened this (someone else may have approved or declined it), so it wasn't declined. Close this and check it."
        : problemText(err, "Couldn't decline the request."));
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit} noValidate>
      {error && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{error}</p>}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" variant="destructive" disabled={busy}>{busy ? "Declining…" : `Decline ${request.email || "the request"}`}</Button>
      </DialogFooter>
    </form>
  );
}
