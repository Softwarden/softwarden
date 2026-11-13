/** The platform console's Sign-in domains page: who claims a work email domain
 *  (`GET platform/sign-in-domains?domain=`, the domain in the URL), the organization that holds it verified and the
 *  pending claims, and the two operator actions, each with a required reason: move it to a claimant
 *  (`POST platform/sign-in-domains/move`; the previous holder loses it) and release it (`POST
 *  platform/sign-in-domains/release`: the verified claim, else the oldest pending one). A fresh DNS proof never takes a
 *  domain from the organization that holds it: support decides here. */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Problem, when } from "@/api";
import { EmptyState, ErrorBanner, ErrorSummary, Field, Loading, PageHead, Select, TableRegion, Textarea } from "@/components/app";
import { chip } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Modal } from "@/features/admin/admin";
import { FocusNote, useFocusNote } from "@/features/admin/org-lifecycle";
import { useUrlFilters } from "@/features/admin/users-access";
import type { Schemas } from "@/lib/openapi";
import { problemText, useApiQuery, useInvalidate } from "@/lib/query";

type Claim = Schemas["PlatformDomainClaim"];
type DomainAction = { kind: "move"; claim: Claim } | { kind: "move-any" } | { kind: "release"; holder: Claim };
type Org = { id: string; name: string; slug: string };

const FILTERS = ["domain"] as const;

export function SignInDomainsPage() {
  const [filters, setFilters] = useUrlFilters(FILTERS);
  const domain = filters.domain.trim().toLowerCase();
  const path = `platform/sign-in-domains?${new URLSearchParams({ domain })}`;
  const q = useApiQuery<Claim[]>(path, { enabled: !!domain, retry: false });
  const invalidate = useInvalidate();
  const note = useFocusNote();
  const [action, setAction] = useState<DomainAction | null>(null);
  const [text, setText] = useState(filters.domain);
  useEffect(() => setText(filters.domain), [filters.domain]);
  const rows = domain ? q.data : undefined;
  const verified = rows?.find((r) => r.status === "verified") ?? null;
  // Release takes the verified claim, else the oldest pending one (the API's order puts it first).
  const releasable = verified ?? rows?.[0] ?? null;
  const open = (a: DomainAction) => { note.show(""); setAction(a); };
  /** Reload the claims, then close: the button that opened the dialog may be gone, so focus goes to the note. */
  const done = async (sentence: string) => { await invalidate(path); note.show(sentence); setAction(null); };
  return (
    <>
      <PageHead title="Sign-in domains" desc="Which organization holds a work email domain on the sign-in page, and who else claims it. An organization's Admin claims a domain in Admin › Single sign-on; on a hosted service it stays pending until its DNS TXT record verifies it, and a verified domain belongs to one organization. A customer who can't verify their own domain comes to support: check who controls it, then move it here." />
      <form className="mb-4 flex flex-wrap items-end gap-3" role="search" aria-label="Look up a sign-in domain"
        onSubmit={(e) => { e.preventDefault(); setFilters({ domain: text.trim().toLowerCase() }); }}>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="domain-q">Domain</Label>
          <Input id="domain-q" className="w-72 font-mono" placeholder="example.com" spellCheck={false} autoComplete="off" value={text} onChange={(e) => setText(e.target.value)} />
        </div>
        <Button type="submit">Look up</Button>
        {domain && <Button type="button" variant="outline" onClick={() => setFilters({ domain: "" })}>Clear</Button>}
      </form>
      <FocusNote ref={note.ref} text={note.text} />
      {!domain && <EmptyState title="Look up a domain">Enter a domain, such as example.com, to see the organization that holds it verified and any pending claims.</EmptyState>}
      {domain && q.isError && <ErrorBanner message={problemText(q.error, "Couldn't look up the domain.")} onRetry={q.error instanceof Problem && q.error.status === 400 ? undefined : () => void q.refetch()} />}
      {domain && q.isPending && <Loading what="the domain's claims" rows={2} />}
      {rows && rows.length === 0 && <EmptyState title={`No organization claims ${domain}`}>Its sign-in addresses find no organization. An organization's Admin can claim it in Admin › Single sign-on, or move it to an organization below.</EmptyState>}
      {rows && (
        // The API's move takes any organization, claimant or not.
        <p className="mb-3 flex flex-wrap items-center gap-3 text-sm">
          <Button type="button" size="sm" variant="outline" onClick={() => open({ kind: "move-any" })}>Move {domain} to another organization…</Button>
          <span className="text-muted-foreground">For an organization that hasn't claimed it; it gets it verified at once.</span>
        </p>
      )}
      {rows && rows.length > 0 && (
        <section aria-labelledby="claims-heading">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-3">
            <h2 id="claims-heading" className="font-display text-section font-semibold">Claims of <span className="font-mono">{domain}</span></h2>
            {releasable && <Button type="button" size="sm" variant="outline" onClick={() => open({ kind: "release", holder: releasable })}>Release {domain}…</Button>}
          </div>
          <p className="mb-2 max-w-prose text-sm" data-testid="domain-holder">
            {verified
              ? <><span aria-hidden="true">✓ </span>{verified.organization.name} holds it: addresses at {domain} find that organization on the sign-in page.</>
              : <><span aria-hidden="true">○ </span>Nobody holds it verified: addresses at {domain} find no organization until a claim is verified or moved.</>}
            {verified && rows.length > 1 && ` ${rows.length - 1} other ${rows.length === 2 ? "organization claims" : "organizations claim"} it too: their Verify is refused in the same words as a missing record.`}
          </p>
          <TableRegion label="Sign-in domain claims table">
            <Table>
              <TableCaption className="sr-only">Every organization's claim of {domain}: the verified holder first, then pending claims, oldest first</TableCaption>
              <TableHeader><TableRow>
                <TableHead scope="col">Organization</TableHead><TableHead scope="col">State</TableHead><TableHead scope="col">Claimed</TableHead>
                <TableHead scope="col">Verified</TableHead><TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((c) => (
                  <TableRow key={c.organization.id} className="align-top" data-status={c.status}>
                    <TableHead scope="row" className="whitespace-normal font-semibold">
                      <Link to={`/platform/organizations/${c.organization.id}`} className="underline underline-offset-2">{c.organization.name}</Link>
                      <span className="block text-xs font-normal text-muted-foreground">{c.organization.slug}</span>
                    </TableHead>
                    <TableCell>{c.status === "verified"
                      ? <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>Verified, holds it</span>
                      : <span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Pending</span>}</TableCell>
                    <TableCell className="tabular">{when(c.claimed_at)}</TableCell>
                    <TableCell className="tabular">{c.verified_at ? when(c.verified_at) : <span className="text-muted-foreground">Not verified</span>}</TableCell>
                    <TableCell className="text-right">
                      {c.status === "pending"
                        ? <Button size="sm" variant="outline" onClick={() => open({ kind: "move", claim: c })}>Move to this organization…<span className="sr-only"> {c.organization.name}</span></Button>
                        : <span className="text-sm text-muted-foreground">Holds it</span>}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
        </section>
      )}
      <Modal open={action !== null} onClose={() => setAction(null)} returnFocusFallback={() => note.ref.current}
        title={action?.kind === "move" ? `Move ${domain} to ${action.claim.organization.name}?` : action?.kind === "move-any" ? `Move ${domain} to another organization?` : action ? `Release ${domain}?` : ""}
        description={action?.kind === "move"
          ? `${action.claim.organization.name} gets ${domain} verified at once${verified ? `, and ${verified.organization.name} loses it` : ""}. Check who controls the domain first, for example by asking them to publish the TXT record their Admin › Single sign-on shows.`
          : action?.kind === "move-any"
            ? `The organization you choose gets ${domain} verified at once${verified ? `, and ${verified.organization.name} loses it` : ""}. Check who controls the domain first.`
            : action ? `${action.holder.organization.name}'s ${action.holder.status === "verified" ? "verified" : "pending"} claim is removed, so the organization that controls ${domain} can verify it.` : undefined}>
        {action && <DomainActionForm domain={domain} action={action} onCancel={() => setAction(null)} onDone={done} />}
      </Modal>
    </>
  );
}

function DomainActionForm({ domain, action, onCancel, onDone }: { domain: string; action: DomainAction; onCancel: () => void; onDone: (sentence: string) => Promise<void> }) {
  const [reason, setReason] = useState("");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [banner, setBanner] = useState("");
  const [saving, setSaving] = useState(false);
  // "Move to another organization": found by name or slug (GET platform/organizations?q=), typed after a pause.
  const [search, setSearch] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => { const t = setTimeout(() => setQ(search.trim()), 300); return () => clearTimeout(t); }, [search]);
  const found = useApiQuery<{ results: Org[] }>(`platform/organizations?${new URLSearchParams(q ? { q } : {})}`, { enabled: action.kind === "move-any" });
  const [chosen, setChosen] = useState("");
  const options = found.data?.results ?? [];
  const target: Org | null = action.kind === "move" ? action.claim.organization : options.find((o) => o.id === chosen) ?? null;
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (saving) return;
    setBanner("");
    const missing: Record<string, string[]> = {};
    if (action.kind === "move-any" && !target) missing.organization_id = ["Choose the organization that gets the domain."];
    if (!reason.trim()) missing.reason = ["Give a reason: the organizations' Admins see it in their audit log."];
    if (Object.keys(missing).length) return setErrors(missing);
    setSaving(true);
    try {
      if ((action.kind === "move" || action.kind === "move-any") && target) {
        await api("platform/sign-in-domains/move", { method: "POST", body: { domain, organization_id: target.id, reason: reason.trim() } });
        await onDone(`${domain} is now verified for ${target.name}.`);
      } else if (action.kind === "release") {
        await api("platform/sign-in-domains/release", { method: "POST", body: { domain, reason: reason.trim() } });
        await onDone(`${domain} is released from ${action.holder.organization.name}.`);
      }
    } catch (err) {
      setSaving(false);
      if (err instanceof Problem && Object.keys(err.errors).length) return setErrors(err.errors);
      setErrors({});
      setBanner(problemText(err, "That didn't work. Nothing was changed; try again."));
    }
  }
  const label = action.kind === "release" ? `Release ${domain}` : target ? `Move ${domain} to ${target.name}` : `Move ${domain}`;
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      {banner && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{banner}</p>}
      {action.kind === "move-any" && (
        <>
          <Field id="organization-q" label="Find the organization" errors={{}} help="Its name or slug.">
            <Input id="organization-q" type="search" autoComplete="off" value={search} onChange={(e) => setSearch(e.target.value)} />
          </Field>
          <Field id="organization_id" label="Organization (required)" errors={errors}
            help={found.isError ? problemText(found.error, "Couldn't load the organizations.") : found.isPending ? "Loading organizations…" : options.length === 0 ? "No organization matches." : `${options.length} shown, by name.`}>
            <Select id="organization_id" value={chosen} onChange={(e) => setChosen(e.target.value)}>
              <option value="">Choose an organization</option>
              {options.map((o) => <option key={o.id} value={o.id}>{o.name} ({o.slug})</option>)}
            </Select>
          </Field>
        </>
      )}
      <Field id="reason" label="Reason (required)" errors={errors} help="A sentence; each organization's Admins see it in their audit log. Up to 500 characters.">
        <Textarea id="reason" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" variant="destructive" aria-busy={saving || undefined}>{saving ? "Working…" : label}</Button>
      </DialogFooter>
    </form>
  );
}
