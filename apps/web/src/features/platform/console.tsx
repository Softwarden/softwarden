/** The platform admin console (docs/API.md "Platform admin console"), for the people who run a hosted
 *  Softwarden: its own area at /platform, outside any organization's shell. Loaded on first visit.
 *
 *  Who sees it: platform staff, signed in with a membership (`auth/me.platform_staff`) or without one (`auth/login`
 *  answered `platform/session`). While impersonating, the console's routes are refused by the API, so the console says
 *  to stop first (the banner's Stop impersonating does it).
 *
 *  - Organizations: search, edition, deletion and suspension filters in the URL, keyset pages, usage columns.
 *  - Organization detail (organization.tsx): members, capabilities, limits, usage, recent events and every action, each
 *    with a required reason and a confirmation.
 *  - Audit events: the platform stream, filtered by organization, action and actor (in the URL).
 *  - System (installation.tsx): the installation's worker, schedules, jobs, backups and warnings.
 *  - Sign-in domains (sign-in-domains.tsx): who claims a domain; move or release it.
 *
 *  With the second factor required (second-factor.tsx) and not yet sent in this session, the console shows only
 *  its setup or code step; `platform/session` says which. */
import { Link, Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useEffect } from "react";
import { byteSize, Problem, PROBLEM_EVENT, SECOND_FACTOR_REQUIRED, signOutRequest, when, type Me, type PlatformSession } from "@/api";
import { orgDate } from "@/components/org-banners";
import { EmptyState, ErrorBanner, Loading, PageHead, Select, TableRegion, UuidGuard } from "@/components/app";
import { BrandMark, chip, SuccessNote, Wordmark } from "@/components/sw";
import { ThemeMenu } from "@/components/theme-menu";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Pager, SearchBox, useUrlFilters } from "@/features/admin/users-access";
import { InstallationSystemPage } from "@/features/platform/installation";
import { OrganizationPage } from "@/features/platform/organization";
import { SecondFactorGate, needsCode } from "@/features/platform/second-factor";
import { SignInDomainsPage } from "@/features/platform/sign-in-domains";
import { problemText, useApiQuery } from "@/lib/query";
import type { Schemas } from "@/lib/openapi";
import type { ConsoleNotice } from "@/App";

export type PlatformOrganization = Schemas["PlatformOrganization"];
export type PlatformEvent = Schemas["PlatformAuditEvent"];
export const EDITIONS: { value: Schemas["EditionEnum"]; label: string }[] = [
  { value: "community", label: "Community" }, { value: "cloud", label: "Cloud" }, { value: "enterprise", label: "Enterprise (reserved)" },
];
export const editionLabel = (e: string) => EDITIONS.find((x) => x.value === e)?.label ?? e;
/** The platform audit stream's actions (GET platform/audit-events `action`), in the API's order. */
const ACTIONS = [
  "backup.key_acknowledged", "capability.granted", "capability.override_removed", "capability.revoked", "export.requested", "impersonation.expired",
  "impersonation.started", "impersonation.stopped", "limit.override_removed", "limit.set", "organization.deletion_cancelled",
  "organization.edition_changed", "organization.suspended", "organization.unsuspended", "sign_in_domain.moved", "sign_in_domain.released", "signin.failures_forgotten",
  "staff.granted", "staff.revoked",
  "staff.second_factor_enrolled", "staff.second_factor_enrollment_started", "staff.second_factor_failed",
  "staff.second_factor_reset", "staff.second_factor_verified", "staff.sign_in_failed", "staff.signed_in",
] as const;

/** A path with the defined query parameters (blank ones left out). */
export function withQuery(path: string, query: Record<string, string>) {
  const search = new URLSearchParams(Object.entries(query).filter(([, v]) => v));
  return search.toString() ? `${path}?${search}` : path;
}

export function PlatformConsole({ me, staff, banner, onStarted, onSignedOut }: {
  me: Me | null | undefined; staff: PlatformSession | null; banner: React.ReactNode;
  /** An impersonation started: reload the session (it is the member's now) and go to their dashboard. */
  onStarted: (impersonationId: string | null) => Promise<void>;
  onSignedOut: () => void;
}) {
  const location = useLocation();
  const notice = (location.state as ConsoleNotice | null)?.notice ?? "";
  const user = staff?.user ?? me?.user;
  // Whether this session sent its second factor. The staff sign-in's answer says so at first; the session is read
  // again for staff with a membership (signed in as a member) and after a code is accepted. Not while impersonating:
  // the console is closed then anyway.
  const session = useApiQuery<PlatformSession>("platform/session", { enabled: !me?.impersonation, ...(staff ? { initialData: staff } : {}) });
  const secondFactor = session.data?.second_factor;
  // A console route refused for the second factor (the operator reset it, or this session's verification ended): read
  // the session again, so the code page shows instead of that page's error.
  const { refetch: refetchSession } = session;
  useEffect(() => {
    const on = (e: Event) => { if ((e as CustomEvent<Problem>).detail.type === SECOND_FACTOR_REQUIRED) void refetchSession(); };
    window.addEventListener(PROBLEM_EVENT, on);
    return () => window.removeEventListener(PROBLEM_EVENT, on);
  }, [refetchSession]);
  async function signOut() {
    await signOutRequest().catch(() => undefined);
    onSignedOut();
  }
  const link = "rounded-md px-3 py-1.5 text-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground aria-[current=page]:bg-accent aria-[current=page]:font-semibold aria-[current=page]:text-accent-foreground";
  return (
    <>
      <a className="absolute -top-12 left-2 z-50 rounded-md bg-primary px-3 py-2 text-primary-foreground focus:top-2" href="#main">Skip to main content</a>
      {banner}
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b bg-card px-4 py-2.5 sm:px-6">
        <span className="flex items-center gap-2">
          <BrandMark className="h-6 w-6" />
          <Wordmark />
          <span className={chip({ state: "neutral" })}>Platform console</span>
        </span>
        {!me?.impersonation && !needsCode(secondFactor) && (
          <nav aria-label="Platform" className="flex flex-wrap gap-1">
            <NavLink to="/platform/organizations" className={link}>Organizations</NavLink>
            <NavLink to="/platform/audit" className={link}>Audit events</NavLink>
            <NavLink to="/platform/sign-in-domains" className={link}>Sign-in domains</NavLink>
            <NavLink to="/platform/system" className={link}>System</NavLink>
          </nav>
        )}
        <span className="ml-auto flex flex-wrap items-center gap-3 text-sm">
          {user && !me?.impersonation && <span className="text-muted-foreground">Signed in as {user.name}</span>}
          {me && !me.impersonation && <Link to="/dashboard">Back to {me.organization.name}</Link>}
          <ThemeMenu />
          <Button type="button" size="sm" variant="outline" onClick={() => void signOut()}>Sign out</Button>
        </span>
      </header>
      <main id="main" tabIndex={-1} className="w-full min-w-0 p-4 sm:p-6">
        <SuccessNote text={notice} />
        {me?.impersonation ? <StopFirst me={me} />
          : session.isPending ? <Loading what="the platform console" />
          : session.isError ? <ErrorBanner message={problemText(session.error, "Couldn't load the platform console.")} onRetry={() => void session.refetch()} />
          : needsCode(secondFactor) ? <SecondFactorGate state={secondFactor!} onVerified={() => session.refetch()} /> : (
          <Routes>
            <Route element={<UuidGuard back={{ to: "/platform/organizations", label: "organizations" }} />}>
            <Route index element={<Navigate to="organizations" replace />} />
            <Route path="organizations" element={<OrganizationsPage />} />
            <Route path="organizations/:id" element={<OrganizationPage onStarted={onStarted} />} />
            <Route path="audit" element={<AuditEventsPage />} />
            <Route path="sign-in-domains" element={<SignInDomainsPage />} />
            <Route path="system" element={<InstallationSystemPage />} />
            <Route path="*" element={<>
              <PageHead title="Page not found" desc="The platform console has no page at this address." />
              <Button asChild variant="outline"><Link to="/platform/organizations">Go to organizations</Link></Button>
            </>} />
            </Route>
          </Routes>
        )}
      </main>
    </>
  );
}

/** While impersonating, every console route but the session and the impersonation is refused: say so. */
function StopFirst({ me }: { me: Me }) {
  return (
    <>
      <PageHead title="Stop impersonating to use the console" />
      <div className="max-w-prose rounded-lg border bg-card px-4 py-5">
        <p>You are acting as {me.user.name} in {me.organization.name}. The platform console is closed until you stop: use Stop impersonating in the banner above.</p>
        <div className="mt-3"><Button asChild variant="outline"><Link to="/dashboard">Back to {me.organization.name}</Link></Button></div>
      </div>
    </>
  );
}

// ---- organizations ------------------------------------------------------------------------------------------------

/** The organization's state, with the deletion date in the organization's timezone; `zone` names the timezone (the
 *  cross-organization list, where each row's differs). */
export function StateChips({ org, zone = false }: { org: Pick<PlatformOrganization, "suspended_at" | "deletion" | "timezone">; zone?: boolean }) {
  return (
    <span className="inline-flex flex-wrap gap-1.5">
      {org.suspended_at && <span className={chip({ state: "overdue" })}><span aria-hidden="true">⊘</span>Suspended</span>}
      {org.deletion && (org.deletion.paused
        // A suspension pauses a scheduled deletion; unsuspending gives back the time that was left.
        ? <span className={chip({ state: "neutral" })}><span aria-hidden="true">⊘</span>Deletion paused (was due {orgDate(org.deletion.due_at, org.timezone)}{zone && ` (${org.timezone})`})</span>
        : <span className={chip({ state: "pending" })}><span aria-hidden="true">!</span>Deletion on {orgDate(org.deletion.due_at, org.timezone)}{zone && ` (${org.timezone})`}</span>)}
      {!org.suspended_at && !org.deletion && <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>Active</span>}
    </span>
  );
}


const FILTERS = ["q", "edition", "deletion", "suspended"] as const;

function OrganizationsPage() {
  const [filters, setFilters] = useUrlFilters(FILTERS);
  const edition = EDITIONS.some((e) => e.value === filters.edition) ? filters.edition : "";
  const deletion = filters.deletion === "scheduled" || filters.deletion === "none" ? filters.deletion : "";
  const suspended = filters.suspended === "true" || filters.suspended === "false" ? filters.suspended : "";
  const q = useApiQuery<Schemas["PaginatedPlatformOrganizationList"]>(withQuery("platform/organizations", { q: filters.q, edition, deletion, suspended, cursor: filters.cursor }));
  const filtered = !!(filters.q || edition || deletion || suspended);
  const clear = () => setFilters({ q: "", edition: "", deletion: "", suspended: "" });
  const rows = q.data?.results;
  return (
    <>
      <PageHead title="Organizations" desc="Every organization on this installation, with its edition, state and the hourly usage snapshot." />
      <div className="mb-3 flex flex-wrap items-end gap-3">
        <SearchBox id="orgs-q" value={filters.q} onChange={(v) => setFilters({ q: v })} placeholder="Name or slug" />
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="orgs-edition">Edition</Label>
          <Select id="orgs-edition" value={edition} onChange={(e) => setFilters({ edition: e.target.value })}>
            <option value="">All</option>
            {EDITIONS.map((e) => <option key={e.value} value={e.value}>{e.label}</option>)}
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="orgs-deletion">Deletion</Label>
          <Select id="orgs-deletion" value={deletion} onChange={(e) => setFilters({ deletion: e.target.value })}>
            <option value="">All</option><option value="scheduled">Scheduled</option><option value="none">Not scheduled</option>
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="orgs-suspended">Suspension</Label>
          <Select id="orgs-suspended" value={suspended} onChange={(e) => setFilters({ suspended: e.target.value })}>
            <option value="">All</option><option value="true">Suspended</option><option value="false">Not suspended</option>
          </Select>
        </div>
        {filtered && <Button type="button" variant="outline" onClick={clear}>Clear filters</Button>}
      </div>
      {q.isError && <ErrorBanner message={problemText(q.error, "Couldn't load the organizations.")} onRetry={() => void q.refetch()} />}
      {q.isPending && <Loading what="organizations" rows={4} />}
      {rows && <p className="sr-only" role="status">{rows.length} {rows.length === 1 ? "organization" : "organizations"} shown</p>}
      {rows && rows.length === 0 && (filtered
        ? <EmptyState title="No organizations match these filters" action={<Button type="button" variant="outline" onClick={clear}>Clear filters</Button>}>Change the search or a filter to see more.</EmptyState>
        : <EmptyState title="No organizations yet">Organizations appear here when someone signs up or an operator creates one.</EmptyState>)}
      {rows && rows.length > 0 && (
        <>
          <TableRegion label="Organizations table">
            <Table>
              <TableCaption className="sr-only">Organizations, sorted by name, with edition, state, members and usage</TableCaption>
              <TableHeader><TableRow>
                <TableHead scope="col">Organization</TableHead><TableHead scope="col">Edition</TableHead><TableHead scope="col">State</TableHead>
                <TableHead scope="col" className="text-right">Members</TableHead><TableHead scope="col" className="text-right">Records</TableHead>
                <TableHead scope="col" className="text-right">Connectors</TableHead><TableHead scope="col" className="text-right">Attachments</TableHead>
                <TableHead scope="col">Last activity</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((o) => (
                  <TableRow key={o.id} className="align-top">
                    <TableHead scope="row" className="min-w-48 whitespace-normal font-semibold">
                      <Link to={`/platform/organizations/${o.id}`} className="underline underline-offset-2">{o.name}</Link>
                      <span className="block text-xs font-normal text-muted-foreground">{o.slug} · created <span className="tabular">{o.created_at.slice(0, 10)}</span></span>
                    </TableHead>
                    <TableCell>{editionLabel(o.edition)}</TableCell>
                    <TableCell><StateChips org={o} zone /></TableCell>
                    <TableCell className="text-right tabular">{o.member_count}</TableCell>
                    {o.usage ? <>
                      <TableCell className="text-right tabular">{o.usage.counts.records_total.toLocaleString("en-US")}</TableCell>
                      <TableCell className="text-right tabular">{o.usage.counts.connectors}</TableCell>
                      <TableCell className="text-right tabular">{byteSize(o.usage.counts.attachment_bytes)}</TableCell>
                    </> : <TableCell colSpan={3} className="text-muted-foreground">Not counted yet (hourly)</TableCell>}
                    <TableCell className="tabular">{o.usage?.last_activity_at ? when(o.usage.last_activity_at) : o.last_activity_at ? when(o.last_activity_at) : "None yet"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
          <p className="mt-2 text-sm text-muted-foreground">Sorted by name. Usage is the hourly snapshot; an organization's page counts it now.</p>
        </>
      )}
      {q.data && <Pager previous={q.data.previous} next={q.data.next} onPage={(cursor) => setFilters({ cursor })} what="Organizations" />}
    </>
  );
}

// ---- audit events -------------------------------------------------------------------------------------------------

/** "key: value" pairs of an event's details, values shortened. */
export function detailsText(details: Record<string, unknown>) {
  const parts = Object.entries(details).map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`);
  const text = parts.join("; ");
  return text.length > 240 ? `${text.slice(0, 239)}…` : text || "—";
}

export function EventsTable({ events, caption, showOrganization = true }: { events: PlatformEvent[]; caption: string; showOrganization?: boolean }) {
  return (
    <TableRegion label={caption}>
      <Table>
        <TableCaption className="sr-only">{caption}</TableCaption>
        <TableHeader><TableRow>
          <TableHead scope="col">When</TableHead><TableHead scope="col">Actor</TableHead><TableHead scope="col">Event</TableHead>
          {showOrganization && <TableHead scope="col">Organization</TableHead>}<TableHead scope="col">Details</TableHead>
        </TableRow></TableHeader>
        <TableBody>
          {events.map((e) => (
            <TableRow key={e.id} className="align-top">
              <TableCell className="tabular">{when(e.at)}</TableCell>
              <TableCell className="whitespace-normal">{e.actor ? e.actor.name : e.actor_email || "The command line"}{e.actor_email && <span className="block text-xs text-muted-foreground">{e.actor_email}</span>}</TableCell>
              <TableCell className="whitespace-normal">{e.label}<span className="block font-mono text-xs text-muted-foreground">{e.action}</span></TableCell>
              {showOrganization && <TableCell>{e.organization_id ? <Link to={`/platform/organizations/${e.organization_id}`}>Open<span className="sr-only"> the organization of this event</span></Link> : "—"}</TableCell>}
              <TableCell className="min-w-56 whitespace-normal text-sm [overflow-wrap:anywhere]">{detailsText(e.details)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableRegion>
  );
}

const AUDIT_FILTERS = ["organization", "action", "actor"] as const;

function AuditEventsPage() {
  const [filters, setFilters] = useUrlFilters(AUDIT_FILTERS);
  const action = (ACTIONS as readonly string[]).includes(filters.action) ? filters.action : "";
  const q = useApiQuery<Schemas["PaginatedPlatformAuditEventList"]>(withQuery("platform/audit-events", { organization: filters.organization, action, actor: filters.actor, cursor: filters.cursor }), { retry: false });
  const filtered = !!(filters.organization || action || filters.actor);
  const clear = () => setFilters({ organization: "", action: "", actor: "" });
  const rows = q.data?.results;
  return (
    <>
      <PageHead title="Platform audit events" desc="Every platform action, newest first: staff, editions, suspensions, deletions canceled, overrides, limits, exports and impersonations." />
      <div className="mb-3 flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="audit-action">Action</Label>
          <Select id="audit-action" value={action} onChange={(e) => setFilters({ action: e.target.value })}>
            <option value="">All</option>
            {ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
          </Select>
        </div>
        <FilterInput id="audit-organization" label="Organization ID" value={filters.organization} onChange={(v) => setFilters({ organization: v })} />
        <FilterInput id="audit-actor" label="Actor's user ID" value={filters.actor} onChange={(v) => setFilters({ actor: v })} />
        {filtered && <Button type="button" variant="outline" onClick={clear}>Clear filters</Button>}
      </div>
      {q.isError && <ErrorBanner message={problemText(q.error, "Couldn't load the platform audit events.")} onRetry={() => void q.refetch()} />}
      {q.isPending && <Loading what="platform audit events" rows={4} />}
      {rows && rows.length === 0 && (filtered
        ? <EmptyState title="No events match these filters" action={<Button type="button" variant="outline" onClick={clear}>Clear filters</Button>}>Change a filter to see more.</EmptyState>
        : <EmptyState title="No platform events yet">Staff sign-ins and every console action are recorded here.</EmptyState>)}
      {rows && rows.length > 0 && <EventsTable events={rows} caption="Platform audit events, newest first" />}
      {q.data && <Pager previous={q.data.previous} next={q.data.next} onPage={(cursor) => setFilters({ cursor })} what="Platform audit events" />}
    </>
  );
}

/** A text filter written to the URL when it changes (on blur or Enter). */
function FilterInput({ id, label, value, onChange }: { id: string; label: string; value: string; onChange: (v: string) => void }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input id={id} key={value} defaultValue={value} className="w-72 font-mono" onBlur={(e) => { if (e.target.value.trim() !== value) onChange(e.target.value.trim()); }}
        onKeyDown={(e) => { if (e.key === "Enter") onChange((e.target as HTMLInputElement).value.trim()); }} />
    </div>
  );
}
