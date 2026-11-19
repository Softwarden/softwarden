/** Administration: People & Teams, Users & roles, Organization settings, and the password screen (E2, E3). */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, Navigate, useLocation, useSearchParams } from "react-router-dom";
import { api, apiWriteWithEtag, DigestRow, DigestStatus, EmailStatus, Person, Preferences, Problem, UserRef, when, type TeamDetail, type Vendor } from "@/api";
import { typedApi, type Schemas } from "@/lib/openapi";
import { canWrite, IMPERSONATING_SIGN_IN_SETTINGS, useMe } from "@/lib/session";
import { problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { KIND_NOUN, type RecordKind } from "@/lib/kinds";
import { zoneOptions } from "@/lib/timezones";
import { RecordLink } from "@/components/record-link";
import {
  attempt, Breadcrumb, Chip, EmptyState, ErrorBanner, ErrorSummary, Field, lazyPart, LazyPart, Loading, PageHead, Select, TableRegion, TabStrip, useLoad, useReturnFocus,
} from "@/components/app";
import { SingleKeySwitch } from "@/components/shortcuts";
import { CheckboxPicker } from "@/components/checkbox-picker";
import { Combobox } from "@/components/combobox";
import { DetailList, DetailRow, GapChip, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { CFValue, CustomFieldInputs, useCustomFieldForm, useCustomFields, type CustomField } from "@/features/catalog/custom-fields";
import { AttachmentsTab, hasAttachments } from "@/features/attachments/tab";
import { StaleBanner } from "@/features/money/contract-ui";
import { ANY_REVISION, ifMatch, staleDeleteText, useOpenedRecord } from "@/lib/if-match";
import { OpenedEdit, type OpenedWrite } from "@/components/opened-edit";

/** Attachment limits and data-quality switches, loaded with the page's first visit. */
const OrganizationExtras = lazyPart(() => import("@/features/admin/org-extras").then((m) => m.OrganizationExtras));
/** Limits, exports and deletion, loaded with the page's first visit. */
const OrganizationLifecycle = lazyPart(() => import("@/features/admin/org-lifecycle").then((m) => m.OrganizationLifecycle));

/** A team, department or vendor row; vendors and teams carry their extra details. */
export type Named = { id: string; name: string; usage: number } & Partial<Omit<Vendor, "id" | "name" | "usage">> & Partial<Pick<TeamDetail, "description" | "members">>;
type Org = {
  id: string; name: string; timezone: string; review_interval_days: number; coordinator: UserRef | null;
  renewal_lead_days: number; reminder_thresholds: number[]; digest_time: string; email: EmailStatus;
  /** Whether the edition includes the compliance profile (capability compliance_profile), and whether it is on. */
  compliance_profile_available: boolean; compliance_profile_enabled: boolean;
  /** An unknown single sign-on identity waits in Users & roles › Pending access instead of being refused. */
  sso_auto_provision: boolean;
};

export function Modal({ title, description, open, onClose, children, keepOpenOnOutside = false, returnFocusFallback }: {
  title: string; description?: string; open: boolean; onClose: () => void; children: React.ReactNode;
  /** While something shown once is in the dialog (an invitation link), Escape and a click outside don't close it. */
  keepOpenOnOutside?: boolean;
  /** Where focus goes on close when the opener is gone (useReturnFocus). */
  returnFocusFallback?: () => HTMLElement | null | undefined;
}) {
  const returnFocus = useReturnFocus(open, returnFocusFallback);
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...returnFocus}
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

// ---- People & Teams ------------------------------------------------------------------

const TABS = ["People", "Teams", "Departments"] as const;

/** /vendors: the vendor list, at an address every role reads as its own rather than an Admin area. It was the
 *  Vendors tab of People & Teams; the old address (`/admin/people?tab=vendors`, its `attachments=<id>` too) redirects. */
export function VendorsPage() {
  return (
    <>
      <PageHead title="Vendors" desc="The companies behind your software: who sells it, its risk file, and the records that use it." />
      <NamedTab kind="Vendors" />
    </>
  );
}

export function PeopleAndTeams() {
  // The active tab lives in the URL (?tab=teams) and tabs follow the arrow-key pattern.
  const [params, setParams] = useSearchParams();
  const { hash } = useLocation();
  // The Vendors tab moved to /vendors: an old link lands there, with its other parameters (an open attachment)
  // and its #fragment.
  if (params.get("tab") === "vendors") {
    const rest = new URLSearchParams(params);
    rest.delete("tab");
    return <Navigate to={{ pathname: "/vendors", search: rest.toString() ? `?${rest}` : "", hash }} replace />;
  }
  const tab = TABS.find((t) => t.toLowerCase() === params.get("tab")) ?? "People";
  const setTab = (t: (typeof TABS)[number]) => setParams(t === "People" ? {} : { tab: t.toLowerCase() });
  function onKeyDown(e: React.KeyboardEvent) {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    const to = e.key === "Home" ? 0 : e.key === "End" ? TABS.length - 1 : (TABS.indexOf(tab) + step + TABS.length) % TABS.length;
    if (!step && e.key !== "Home" && e.key !== "End") return;
    e.preventDefault();
    setTab(TABS[to]);
    document.getElementById(`tab-${TABS[to]}`)?.focus();
  }
  return (
    <>
      {/* Each tab has its own document title, so tabs, history and screen readers tell them apart. */}
      <PageHead title="People & Teams" docTitle={`${tab} · People & Teams`} desc="People can own and confirm records without having an account." />
      <div className="mb-4 flex gap-1 overflow-x-auto border-b" role="tablist" aria-label="People & Teams" onKeyDown={onKeyDown}>
        {TABS.map((t) => (
          <button key={t} role="tab" type="button" aria-selected={tab === t} id={`tab-${t}`} aria-controls="tabpanel" tabIndex={tab === t ? 0 : -1}
            onClick={() => setTab(t)}
            className="-mb-px whitespace-nowrap border-b-[3px] border-transparent px-3 py-2 text-sm text-muted-foreground aria-selected:border-accent-line aria-selected:font-semibold aria-selected:text-link">
            {t}
          </button>
        ))}
      </div>
      <div role="tabpanel" id="tabpanel" aria-labelledby={`tab-${tab}`}>
        {tab === "People" ? <PeopleTab /> : <NamedTab key={tab} kind={tab} />}
      </div>
    </>
  );
}

type RecordRef = { id: string; kind: RecordKind; name: string; archived: boolean };
type PersonRow = Person & { title: string; anonymized: boolean; user: UserRef | null; responsible: number };
type PersonDetail = PersonRow & {
  business_owner: RecordRef[]; technical_owner: RecordRef[]; confirmed: RecordRef[]; review_count: number; latest_review: string | null;
  renewals?: { id: string; name: string; archived: boolean }[];
};
type PersonAction = { kind: "view" | "edit" | "inactive" | "anonymize"; person: PersonRow } | { kind: "new" } | null;

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

function PeopleTab() {
  const me = useMe();
  const people = useLoad<PersonRow[]>("people");
  const departments = useLoad<Named[]>("departments");
  const [action, setAction] = useState<PersonAction>(null);
  const [filter, setFilter] = useState({ q: "", department: "", status: "all" });
  const [notice, setNotice] = useState("");
  const close = () => setAction(null);
  const done = (text: string) => { setAction(null); setNotice(text); people.reload(); };
  async function reactivate(p: PersonRow) {
    const failed = await attempt(() => api(`people/${p.id}`, { method: "PATCH", body: { active: true }, headers: ANY_REVISION }));
    done(failed ? failed.detail : `${p.name} is active again and can be chosen as an owner.`);
  }
  if (people.error) return <ErrorBanner message={people.error} onRetry={people.reload} />;
  if (!people.data) return <Loading what="people" />;
  const q = filter.q.trim().toLowerCase();
  const shown = people.data.filter((p) =>
    (!q || p.name.toLowerCase().includes(q) || p.email.toLowerCase().includes(q))
    && (!filter.department || p.department?.id === filter.department)
    && (filter.status === "all" || p.active === (filter.status === "active")));
  const person = action && "person" in action ? action.person : null;
  return (
    <>
      <SuccessNote text={notice} />
      {people.data.length === 0 ? (
        <EmptyState title="No people yet" action={canWrite(me) && <Button onClick={() => setAction({ kind: "new" })}>Add person</Button>}>
          {canWrite(me) ? "People are owners and contacts; they don't need a login. Add one here to start assigning owners." : "People are owners and contacts; Editors and Admins add them."}
        </EmptyState>
      ) : <>
        <div className="mb-3 flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="people-q">Search</Label>
            <Input id="people-q" type="search" className="w-64" placeholder="Name or email" value={filter.q} onChange={(e) => setFilter({ ...filter, q: e.target.value })} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="people-department">Department</Label>
            <Select id="people-department" value={filter.department} onChange={(e) => setFilter({ ...filter, department: e.target.value })}>
              <option value="">All departments</option>
              {(departments.data ?? []).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="people-status">Status</Label>
            <Select id="people-status" value={filter.status} onChange={(e) => setFilter({ ...filter, status: e.target.value })}>
              <option value="all">All</option><option value="active">Active</option><option value="inactive">Inactive</option>
            </Select>
          </div>
          {canWrite(me) && <Button className="ml-auto" onClick={() => setAction({ kind: "new" })}>Add person</Button>}
        </div>
        <p className="sr-only" role="status">{plural(shown.length, "person", "people")} shown</p>
        {shown.length === 0 ? <EmptyState title="No one matches">Change the search or filters to see more people.</EmptyState> : (
          <TableRegion label="People table">
            <Table>
              <TableCaption className="sr-only">People</TableCaption>
              <TableHeader><TableRow><TableHead>Name</TableHead><TableHead>Email</TableHead><TableHead>Department</TableHead><TableHead>Status</TableHead><TableHead>Linked user</TableHead><TableHead>Responsible</TableHead><TableHead><span className="sr-only">Actions</span></TableHead></TableRow></TableHeader>
              <TableBody>
                {shown.map((p) => (
                  <TableRow key={p.id}>
                    <TableHead scope="row" className="min-w-40 max-w-sm whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
                      <button type="button" className={`text-left underline-offset-2 hover:underline ${p.anonymized ? "italic" : ""}`} onClick={() => setAction({ kind: "view", person: p })}>{p.name}</button>
                      {p.title && <span className="block text-xs font-normal text-muted-foreground">{p.title}</span>}
                    </TableHead>
                    <TableCell>{p.email || "—"}</TableCell>
                    <TableCell>{p.department?.name ?? "—"}</TableCell>
                    <TableCell>{p.active ? "Active" : <Chip>Inactive</Chip>}</TableCell>
                    <TableCell>{p.user?.name ?? "—"}</TableCell>
                    <TableCell>{p.responsible ? plural(p.responsible, "record", "records") : "0"}</TableCell>
                    <TableCell className="text-right">
                      {canWrite(me) && !p.anonymized && <Button size="sm" variant="outline" onClick={() => setAction({ kind: "edit", person: p })}>Edit<span className="sr-only"> {p.name}</span></Button>}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
        )}
      </>}
      <Modal title={person?.name ?? ""} open={action?.kind === "view"} onClose={close}>
        {person && <PersonPanel person={person} onAction={(kind) => setAction({ kind, person })} onReactivate={() => reactivate(person)} />}
      </Modal>
      <Modal title={action?.kind === "new" ? "Add person" : `Edit ${person?.name ?? ""}`} open={action?.kind === "new" || action?.kind === "edit"} onClose={close}
        description="Only the name is required. People don't need an account to own or confirm records.">
        {action?.kind === "new" && <PersonForm person={null} departments={departments.data ?? []} onCancel={close} onDone={done} />}
        {action?.kind === "edit" && person && (
          <OpenedEdit<PersonRow> path={`people/${person.id}`} what="person">
            {(p, write) => <PersonForm person={p} write={write} departments={departments.data ?? []} onCancel={close} onDone={done} />}
          </OpenedEdit>
        )}
      </Modal>
      <Modal title={`Mark ${person?.name ?? ""} inactive?`} open={action?.kind === "inactive"} onClose={close}>
        {person && <MarkInactiveForm person={person} onCancel={close} onDone={done} />}
      </Modal>
      <Modal title={`Anonymize ${person?.name ?? ""}`} open={action?.kind === "anonymize"} onClose={close}
        description="This removes personal data and can't be undone.">
        {person && <AnonymizeForm person={person} onCancel={close} onDone={done} />}
      </Modal>
    </>
  );
}

/** The person panel: details and everything they're responsible for. */
function PersonPanel({ person, onAction, onReactivate }: {
  person: PersonRow; onAction: (kind: "edit" | "inactive" | "anonymize") => void; onReactivate: () => void;
}) {
  const me = useMe();
  const detail = useLoad<PersonDetail>(`people/${person.id}`);
  if (detail.error) return <ErrorBanner message={detail.error} onRetry={detail.reload} />;
  if (!detail.data) return <Loading what="this person" />;
  const p = detail.data;
  const records = (label: string, list: RecordRef[]) => (
    <DetailRow label={label}>
      {list.length === 0 ? "—" : (
        <ul className="flex flex-col gap-0.5">
          {list.map((r) => (
            <li key={r.id}><RecordLink record={r} className="inline-block min-h-6 leading-6" />{r.kind !== "application" && <span className="text-muted-foreground"> ({KIND_NOUN[r.kind]?.[0] ?? r.kind})</span>}{r.archived && <span className="text-muted-foreground"> (archived)</span>}</li>
          ))}
        </ul>
      )}
    </DetailRow>
  );
  return (
    <>
      <DetailList className="mb-4 rounded-md border px-3">
        <DetailRow label="Email">{p.email || "—"}</DetailRow>
        <DetailRow label="Title">{p.title || "—"}</DetailRow>
        <DetailRow label="Department">{p.department?.name ?? "—"}</DetailRow>
        <DetailRow label="Status">{p.anonymized ? "Anonymized (inactive)" : p.active ? "Active" : "Inactive"}</DetailRow>
        <DetailRow label="Linked user">{p.user?.name ?? "None (owners don't need a login)"}</DetailRow>
        {records("Business owner of", p.business_owner)}
        {records("Technical owner of", p.technical_owner)}
        <DetailRow label="Confirmed reviews">{p.review_count ? `${p.review_count} (latest ${p.latest_review})` : "None"}</DetailRow>
        {records("Confirmed records", p.confirmed)}
        <DetailRow label="Renewal responsible for">
          {!p.renewals?.length ? "—" : (
            <ul className="flex flex-col gap-0.5">
              {p.renewals.map((x) => <li key={x.id}><Link to={`/renewals/${x.id}`} className="inline-block min-h-6 leading-6">{x.name}</Link>{x.archived && <span className="text-muted-foreground"> (archived)</span>}</li>)}
            </ul>
          )}
        </DetailRow>
      </DetailList>
      <p className="mb-4"><Link to={`/people/${p.id}`} className="underline">Where {p.name} appears{canWrite(me) ? " and reassign their roles" : ""}</Link></p>
      {!p.anonymized && (canWrite(me) || me.role === "admin") && (
        <DialogFooter className="flex-wrap">
          {me.role === "admin" && <Button variant="outline" className="text-error-foreground" onClick={() => onAction("anonymize")}>Anonymize person</Button>}
          {canWrite(me) && (p.active
            ? <Button variant="outline" onClick={() => onAction("inactive")}>Mark inactive</Button>
            : <Button variant="outline" onClick={onReactivate}>Mark active</Button>)}
          {canWrite(me) && <Button onClick={() => onAction("edit")}>Edit</Button>}
        </DialogFooter>
      )}
    </>
  );
}

function PersonForm({ person, write, departments, onCancel, onDone }: {
  person: PersonRow | null; write?: OpenedWrite; departments: Named[]; onCancel: () => void; onDone: (text: string) => void;
}) {
  const me = useMe();
  const [form, setForm] = useState({
    name: person?.name ?? "", email: person?.email ?? "", title: person?.title ?? "",
    department: person?.department?.id ?? "", user: person?.user ? String(person.user.id) : "",
  });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    // Only Admins can see the user list, so only they send the linked user.
    const { user, ...rest } = form;
    const body = { ...rest, department: form.department || null, ...(me.role === "admin" ? { user: user ? Number(user) : null } : {}) };
    const failed = await attempt(async () => person
      ? api(`people/${person.id}`, { method: "PATCH", body, headers: await write?.ifMatch() })
      : api("people", { method: "POST", body }));
    if (failed?.status === 412 && write) return write.onStale();
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { name: [failed.detail] });
    onDone(person ? `${form.name} saved.` : `${form.name} added.`);
  }
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      <Field id="name" label="Name (required)" errors={errors}><Input id="name" value={form.name} onChange={set("name")} /></Field>
      <Field id="email" label="Email" errors={errors} help="Optional. Two people can't share an email."><Input id="email" type="email" value={form.email} onChange={set("email")} /></Field>
      <Field id="title" label="Title" errors={errors}><Input id="title" value={form.title} onChange={set("title")} /></Field>
      <Field id="department" label="Department" errors={errors}>
        <Select id="department" value={form.department} onChange={set("department")}>
          <option value="">None</option>
          {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </Select>
      </Field>
      {me.role === "admin" && <LinkedUserField value={form.user} onChange={set("user")} errors={errors} />}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit">Save</Button>
      </DialogFooter>
    </form>
  );
}

function LinkedUserField({ value, onChange, errors }: {
  value: string; onChange: (e: React.ChangeEvent<HTMLSelectElement>) => void; errors: Record<string, string[]>;
}) {
  const users = useLoad<Member[]>("users");
  return (
    <Field id="user" label="Linked user" errors={errors} help="Optional. Link the person to their Softwarden account, if they have one.">
      <Select id="user" value={value} onChange={onChange}>
        <option value="">None</option>
        {(users.data ?? []).map((u) => <option key={u.id} value={u.id}>{u.name} ({u.email})</option>)}
      </Select>
    </Field>
  );
}

/** Owned-record count read fresh when the dialog opens; the list may be older than the dialog. */
function useResponsible(person: PersonRow) {
  return useLoad<PersonDetail>(`people/${person.id}`).data?.responsible ?? person.responsible;
}

function MarkInactiveForm({ person, onCancel, onDone }: { person: PersonRow; onCancel: () => void; onDone: (t: string) => void }) {
  const responsible = useResponsible(person);
  const [error, setError] = useState("");
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const failed = await attempt(() => api(`people/${person.id}`, { method: "PATCH", body: { active: false }, headers: ANY_REVISION }));
    if (failed) return setError(failed.detail);
    onDone(`${person.name} is inactive.`);
  }
  return (
    <form onSubmit={submit} noValidate>
      {error && <p className="mb-3 text-sm text-error-foreground" role="alert">{error}</p>}
      <p className="mb-4">
        {responsible
          ? `${person.name} owns ${plural(responsible, "record", "records")}. Marking inactive creates ownership gaps and marks those records Needs confirmation.`
          : `${person.name} doesn't own any records.`}
        {" "}Inactive people aren't offered as owners.
      </p>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit">Mark inactive</Button>
      </DialogFooter>
    </form>
  );
}

function AnonymizeForm({ person, onCancel, onDone }: { person: PersonRow; onCancel: () => void; onDone: (t: string) => void }) {
  const responsible = useResponsible(person);
  const [typed, setTyped] = useState("");
  const [error, setError] = useState("");
  const matches = typed.trim() === person.name;
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!matches) return;
    const failed = await attempt(() => api(`people/${person.id}/anonymize`, { method: "POST", body: { confirm_name: typed.trim() } }));
    if (failed) return setError(Object.values(failed.errors)[0]?.join(" ") ?? failed.detail);
    onDone(`${person.name} was anonymized.`);
  }
  return (
    <form onSubmit={submit} noValidate>
      {error && <p className="mb-3 text-sm text-error-foreground" role="alert">{error}</p>}
      <ul className="mb-4 ml-5 list-disc space-y-1 text-sm">
        <li>Their name becomes "Former person #n"; email, title, department and linked user are cleared.</li>
        <li>They become inactive.{responsible > 0 && ` ${plural(responsible, "record becomes an ownership gap", "records become ownership gaps")} and Needs confirmation.`}</li>
        <li>Review history is kept and shows them as Former person.</li>
        <li>Stored import files and error reports that mention them are deleted now.</li>
      </ul>
      <Field id="confirm_name" label="Type the person's name to confirm">
        <Input id="confirm_name" autoComplete="off" value={typed} onChange={(e) => setTyped(e.target.value)} />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" variant="destructive" disabled={!matches} aria-describedby="anonymize-why">Anonymize {person.name}</Button>
      </DialogFooter>
      <p id="anonymize-why" className="sr-only">{matches ? "" : `Type ${person.name} above to enable this button.`}</p>
    </form>
  );
}

// Per kind: singular noun, what uses it, and the column that counts those uses.
const NAMED: Record<"Teams" | "Departments" | "Vendors", { noun: string; one: string; many: string; column: string; empty: string }> = {
  Teams: { noun: "team", one: "record", many: "records", column: "Supports", empty: "Add a team to use it as a record's support team." },
  Departments: { noun: "department", one: "person", many: "people", column: "People", empty: "Add a department to label people with it." },
  Vendors: { noun: "vendor", one: "application or renewal", many: "applications and renewals", column: "Used by", empty: "Add a vendor to pick it on applications and renewals." },
};

function NamedTab({ kind }: { kind: "Teams" | "Departments" | "Vendors" }) {
  const me = useMe();
  const path = kind.toLowerCase();
  const items = useLoad<Named[]>(path);
  // Vendors carry custom fields: a column summing up the set ones, and inputs in the form.
  const custom = useCustomFields("vendor", kind === "Vendors");
  const vendorFields = kind === "Vendors" ? custom.fields ?? [] : [];
  const [editing, setEditing] = useState<Named | "new" | null>(null);
  const [deleting, setDeleting] = useState<Named | null>(null);
  const [notice, setNotice] = useState("");
  // A vendor's attachments open in a dialog named in the URL (/vendors?attachments=<id>), so search
  // results and shared links can open it.
  const [params, setParams] = useSearchParams();
  const attachable = kind === "Vendors" && hasAttachments(me);
  const showAttachments = (vendorId: string | null) => setParams((cur) => {
    const next = new URLSearchParams(cur);
    if (vendorId) next.set("attachments", vendorId); else next.delete("attachments");
    return next;
  }, { replace: true });
  const { noun, one, many, column, empty } = NAMED[kind];
  const done = (text: string) => { setEditing(null); setDeleting(null); setNotice(text); items.reload(); };
  if (items.error) return <ErrorBanner message={items.error} onRetry={items.reload} />;
  if (!items.data) return <Loading what={path} />;
  const add = canWrite(me) && <Button onClick={() => setEditing("new")}>Add {noun}</Button>;
  return (
    <>
      <SuccessNote text={notice} />
      {items.data.length === 0 ? <EmptyState title={`No ${path} yet`} action={add}>{canWrite(me) ? empty : `Editors and Admins can add ${path}.`}</EmptyState> : <>
        {add && <div className="mb-3 flex justify-end">{add}</div>}
        <TableRegion label={`${kind} table`}>
          <Table>
            <TableCaption className="sr-only">{kind}</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Name</TableHead>
              {kind === "Teams" && <TableHead scope="col">Description</TableHead>}
              {kind === "Teams" && <TableHead scope="col">Members</TableHead>}
              {kind === "Vendors" && <TableHead scope="col">Website</TableHead>}
              {kind === "Vendors" && <TableHead scope="col">Support</TableHead>}
              {kind === "Vendors" && <TableHead scope="col">Account manager</TableHead>}
              {vendorFields.length > 0 && <TableHead scope="col">Custom fields</TableHead>}
              <TableHead scope="col">{column}</TableHead>
              <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {items.data.map((t) => (
                <TableRow key={t.id}>
                  <TableHead scope="row" className="min-w-40 max-w-sm whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
                    {/* A vendor has its own page (details, risk file and attachments). */}
                    {kind === "Vendors" ? <Link to={`/vendors/${t.id}`} className="underline">{t.name}</Link> : t.name}
                  </TableHead>
                  {kind === "Teams" && <TableCell className="max-w-md whitespace-normal">{t.description || "—"}</TableCell>}
                  {kind === "Teams" && <TableCell className="max-w-sm whitespace-normal">{t.members?.length ? t.members.map((m) => m.name).join(", ") : <span className="text-muted-foreground">No members recorded</span>}</TableCell>}
                  {kind === "Vendors" && <TableCell className="[overflow-wrap:anywhere]">{t.website || "—"}</TableCell>}
                  {kind === "Vendors" && <TableCell className="whitespace-normal">{[t.support_email, t.support_phone].filter(Boolean).join(" · ") || (t.support_url ? <a href={t.support_url} target="_blank" rel="noreferrer noopener">Support site<span className="sr-only"> for {t.name} (opens in a new tab)</span></a> : "—")}</TableCell>}
                  {kind === "Vendors" && <TableCell>{t.account_manager ? <>{t.account_manager.name}{!t.account_manager.active && <span className="text-muted-foreground"> (inactive)</span>}</> : "—"}</TableCell>}
                  {vendorFields.length > 0 && <TableCell className="min-w-56 whitespace-normal"><VendorCustomValues fields={vendorFields} values={t.custom_field_data} /></TableCell>}
                  <TableCell>{plural(t.usage, one, many)}</TableCell>
                  <TableCell className="space-x-2 text-right">
                    {attachable && <Button size="sm" variant="outline" onClick={() => showAttachments(t.id)}>Attachments<span className="sr-only"> of {t.name}</span></Button>}
                    {canWrite(me) && <>
                      <Button size="sm" variant="outline" onClick={() => setEditing(t)}>Edit<span className="sr-only"> {t.name}</span></Button>
                      {/* Delete is offered only when nothing uses it; the API still refuses a race. */}
                      {t.usage === 0 && <Button size="sm" variant="outline" onClick={() => setDeleting(t)}>Delete<span className="sr-only"> {t.name}</span></Button>}
                    </>}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
        {kind === "Teams" && <p className="mt-2 text-sm text-muted-foreground">Members are optional and for reference. A team can be a support team, never an owner.</p>}
      </>}
      <Modal title={editing === "new" ? `Add ${noun}` : `Edit ${editing ? editing.name : ""}`} open={editing !== null} onClose={() => setEditing(null)}>
        {editing && (kind !== "Vendors" || custom.fields
          ? <NamedForm path={path} kind={kind} item={editing === "new" ? null : editing} onCancel={() => setEditing(null)} onDone={done} customFields={vendorFields} />
          : custom.error ? <ErrorBanner message={problemText(custom.error, "Couldn't load the custom fields.")} onRetry={custom.refetch} /> : <Loading what="form" />)}
      </Modal>
      {attachable && <VendorAttachments vendor={items.data.find((v) => v.id === params.get("attachments")) ?? null} onClose={() => showAttachments(null)} />}
      <Modal title={`Delete ${deleting?.name ?? ""}?`} open={deleting !== null} onClose={() => setDeleting(null)}
        description={`Nothing uses this ${noun}. Deleting it can't be undone.`}>
        {deleting && <DeleteForm path={path} item={deleting} onCancel={() => setDeleting(null)} onDone={done} />}
      </Modal>
    </>
  );
}

/** A vendor's attachments in a dialog over the Vendors tab. */
function VendorAttachments({ vendor, onClose }: { vendor: Named | null; onClose: () => void }) {
  const open = vendor !== null;
  const returnFocus = useReturnFocus(open);
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-3xl" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Attachments of {vendor?.name}</DialogTitle>
          <DialogDescription>Contracts, invoices, security reviews and other documents about this vendor, as files or links.</DialogDescription>
        </DialogHeader>
        {vendor && <AttachmentsTab parent={{ type: "vendor", id: vendor.id, name: vendor.name }} />}
        <DialogFooter><Button type="button" variant="outline" onClick={onClose}>Close</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** A vendor's custom field values in a table cell: the ones that are set, as "Label: value". */
function VendorCustomValues({ fields, values }: { fields: CustomField[]; values?: Record<string, unknown> }) {
  const set = fields.filter((cf) => values?.[cf.name] !== null && values?.[cf.name] !== undefined);
  if (!set.length) return <span className="text-muted-foreground">None set</span>;
  return (
    <dl className="m-0 grid gap-0.5 text-[13px]">
      {set.map((cf) => <div key={cf.name}><dt className="inline text-muted-foreground">{cf.label}: </dt><dd className="inline"><CFValue cf={cf} value={values![cf.name]} compact /></dd></div>)}
    </dl>
  );
}

/** "+ Add vendor" from another form (applications, renewals). A plain name is enough unless the organization has a
 *  required vendor custom field, or the API refuses the name alone over a custom field: then the vendor dialog opens
 *  with the typed name, so the custom fields can be filled in. `onAdded` gets the saved vendor; focus goes back to
 *  `focusId` (the vendor picker) when the dialog closes. */
export function useVendorQuickAdd({ focusId, onAdded, onError, onDialog }: {
  focusId: string; onAdded: (vendor: { id: string; name: string }) => void; onError: (text: string) => void; onDialog?: () => void;
}) {
  const custom = useCustomFields("vendor");
  const [name, setName] = useState<string | null>(null);
  const open = (n: string) => { setName(n); onDialog?.(); };
  const close = () => { setName(null); setTimeout(() => document.getElementById(focusId)?.focus()); };
  async function add(typed: string) {
    if (custom.fields?.some((cf) => cf.required)) return open(typed);
    try {
      onAdded(await api<{ id: string; name: string }>("vendors", { method: "POST", body: { name: typed } }));
    } catch (e) {
      if (e instanceof Problem && Object.keys(e.errors).some((k) => k.startsWith("custom_field_data."))) return open(typed);
      onError(e instanceof Problem ? (e.errors.name?.[0] ?? e.detail) : "Couldn't add the vendor. Try again.");
    }
  }
  const dialog = (
    <Modal title="Add vendor" open={name !== null} onClose={close}
      description="Vendors in this organization have custom fields to fill in. The vendor is saved to Vendors and chosen here.">
      {name !== null && (custom.fields
        ? <NamedForm path="vendors" kind="Vendors" item={null} initialName={name} customFields={custom.fields} onCancel={close} idPrefix="new-vendor-"
          onSaved={(v) => { onAdded(v); close(); }} onDone={() => undefined} />
        : custom.error ? <ErrorBanner message={problemText(custom.error, "Couldn't load the custom fields.")} onRetry={custom.refetch} /> : <Loading what="the form" />)}
    </Modal>
  );
  return { add, dialog };
}

type NamedFormProps = {
  path: string; kind: string; item: Named | null; onCancel: () => void; onDone: (t: string) => void; customFields: CustomField[];
  /** A new item's name, typed elsewhere ("+ Add vendor"). */
  initialName?: string;
  /** Called with the saved item before `onDone`. */
  onSaved?: (saved: { id: string; name: string }) => void;
  /** Prefix for element IDs, when the form opens over another form with the same field IDs ("+ Add vendor"). */
  idPrefix?: string;
};

/** The add or edit form of a team, department or vendor (the vendor page's Edit opens it too). An edit reads the item
 *  with its ETag when it opens and saves with If-Match. */
export function NamedForm(props: NamedFormProps) {
  if (!props.item) return <NamedFields {...props} />;
  return (
    <OpenedEdit<Named> path={`${props.path}/${props.item.id}`} what={props.kind.slice(0, -1).toLowerCase()}>
      {(item, write) => <NamedFields {...props} item={item} write={write} />}
    </OpenedEdit>
  );
}

function NamedFields({ path, kind, item, write, onCancel, onDone, customFields, initialName, onSaved, idPrefix = "" }: NamedFormProps & { write?: OpenedWrite }) {
  const [form, setForm] = useState({
    name: item?.name ?? initialName ?? "", website: item?.website ?? "", description: item?.description ?? "",
    support_url: item?.support_url ?? "", support_email: item?.support_email ?? "", support_phone: item?.support_phone ?? "",
    portal_url: item?.portal_url ?? "", account_manager: item?.account_manager?.id ?? "", notes: item?.notes ?? "",
  });
  const [members, setMembers] = useState<string[]>(() => (item?.members ?? []).map((m) => m.id));
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const custom = useCustomFieldForm(customFields, item ? "edit" : "create", item?.custom_field_data);
  // People for the account manager and the member list (active ones, plus whoever is already chosen).
  const peopleQuery = useApiQuery<Person[]>("people", { enabled: kind !== "Departments" });
  const people = { data: peopleQuery.data, error: peopleQuery.isError ? problemText(peopleQuery.error, "Couldn't load people.") : "", reload: () => void peopleQuery.refetch() };
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setForm({ ...form, [k]: e.target.value });
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const cf = custom.build();
    if (!form.name.trim() || Object.keys(cf.errors).length) return setErrors({ ...(form.name.trim() ? {} : { name: ["Enter a name."] }), ...cf.errors });
    const t = (v: string) => v.trim();
    const body: Record<string, unknown> = kind === "Vendors"
      ? {
        name: t(form.name), website: t(form.website), support_url: t(form.support_url), support_email: t(form.support_email),
        support_phone: t(form.support_phone), portal_url: t(form.portal_url), account_manager: form.account_manager || null, notes: t(form.notes),
      }
      : kind === "Teams" ? { name: t(form.name), description: t(form.description), members } : { name: t(form.name) };
    if (cf.data) body.custom_field_data = cf.data;
    let saved: { id: string; name: string } | undefined;
    const failed = await attempt(async () => { saved = await (item ? api<{ id: string; name: string }>(`${path}/${item.id}`, { method: "PATCH", body, headers: await write?.ifMatch() }) : api<{ id: string; name: string }>(path, { method: "POST", body })); });
    if (failed?.status === 412 && write) return write.onStale();
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { name: [failed.detail] });
    if (saved) onSaved?.(saved);
    onDone(`${form.name.trim()} saved.`);
  }
  // Errors are kept under the API's keys; shown under the prefixed element IDs.
  const shownErrors = idPrefix ? Object.fromEntries(Object.entries(errors).map(([k, v]) => [`${idPrefix}${k}`, v])) : errors;
  const chosen = new Set([...members, form.account_manager]);
  const offered = (people.data ?? []).filter((p) => p.active || chosen.has(p.id));
  const textarea = "w-full min-w-0 rounded-md border border-input bg-transparent px-3 py-2 text-base shadow-xs outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 aria-invalid:border-destructive md:text-sm";
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={shownErrors} focusOnShow={kind === "Vendors" ? "field" : false} />
      <Field id={`${idPrefix}name`} label="Name (required)" errors={shownErrors}><Input id={`${idPrefix}name`} value={form.name} onChange={set("name")} /></Field>
      {kind === "Teams" && <>
        <Field id={`${idPrefix}description`} label="Description" errors={shownErrors}>
          <textarea id={`${idPrefix}description`} rows={3} value={form.description} onChange={set("description")} className={textarea} />
        </Field>
        {people.error ? <ErrorBanner message={people.error} onRetry={people.reload} /> : !people.data ? <Loading what="people" /> : (
          <CheckboxPicker id={`${idPrefix}members`} legend="Members" value={members} onChange={setMembers} errors={shownErrors[`${idPrefix}members`]}
            options={offered.map((p) => ({ id: p.id, label: p.name + (p.active ? "" : " (inactive)"), hint: p.department?.name }))}
            help="Optional. The people in the team, for reference; a team can't own a record."
            empty={<p className="text-sm text-muted-foreground">No people yet. Add people on the People tab first.</p>} />
        )}
      </>}
      {kind === "Vendors" && <>
        <Field id={`${idPrefix}website`} label="Website" errors={shownErrors} help="Optional, for example https://zylker.example"><Input id={`${idPrefix}website`} type="url" value={form.website} onChange={set("website")} /></Field>
        <Field id={`${idPrefix}portal_url`} label="Customer portal" errors={shownErrors} help="The vendor's admin or customer portal."><Input id={`${idPrefix}portal_url`} type="url" value={form.portal_url} onChange={set("portal_url")} /></Field>
        <Field id={`${idPrefix}support_url`} label="Support site" errors={shownErrors}><Input id={`${idPrefix}support_url`} type="url" value={form.support_url} onChange={set("support_url")} /></Field>
        <Field id={`${idPrefix}support_email`} label="Support email" errors={shownErrors}><Input id={`${idPrefix}support_email`} type="email" value={form.support_email} onChange={set("support_email")} /></Field>
        <Field id={`${idPrefix}support_phone`} label="Support phone" errors={shownErrors}><Input id={`${idPrefix}support_phone`} type="tel" value={form.support_phone} onChange={set("support_phone")} /></Field>
        {people.error ? <ErrorBanner message={people.error} onRetry={people.reload} /> : (
          <Field id={`${idPrefix}account_manager`} label="Account manager" errors={shownErrors} help="A person in People & Teams, for example the vendor's contact.">
            <Combobox id={`${idPrefix}account_manager`} options={offered.map((p) => ({ id: p.id, label: p.name, hint: p.email || undefined }))} value={form.account_manager}
              onChange={(v) => setForm({ ...form, account_manager: v })} placeholder={people.data ? "Search people…" : "Loading people…"} clearLabel="No account manager"
              listLabel="Account manager suggestions" />
          </Field>
        )}
        <Field id={`${idPrefix}notes`} label="Notes" errors={shownErrors} help="Optional, up to 5,000 characters.">
          <textarea id={`${idPrefix}notes`} rows={3} value={form.notes} onChange={set("notes")} className={textarea} />
        </Field>
        <CustomFieldInputs form={custom} errors={shownErrors} idPrefix={idPrefix} />
      </>}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit">Save</Button>
      </DialogFooter>
    </form>
  );
}

function DeleteForm({ path, item, onCancel, onDone }: { path: string; item: Named; onCancel: () => void; onDone: (t: string) => void }) {
  const [error, setError] = useState("");
  // Read with its ETag as the confirmation opens; the delete sends it as If-Match.
  const opened = useOpenedRecord(`${path}/${item.id}`);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const failed = await attempt(async () => api(`${path}/${item.id}`, { method: "DELETE", headers: ifMatch(await opened.etag()) }));
    if (failed?.status === 412) return setError(staleDeleteText(path.slice(0, -1)));
    // A refusal names what uses it, e.g. "… 2 applications use it (A, B). Reassign them first."
    if (failed) return setError(failed.detail);
    onDone(`${item.name} deleted.`);
  }
  return (
    <form onSubmit={submit} noValidate>
      {error && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{error}</p>}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        {!error && <Button type="submit" variant="destructive">Delete {item.name}</Button>}
      </DialogFooter>
    </form>
  );
}

// ---- Users & roles (Admin) ---------------------------------------------------------------

/** Invitations and pending access, loaded when their tab is first opened. */
const InvitationsTab = lazyPart(() => import("@/features/admin/users-access").then((m) => m.InvitationsTab));
const PendingAccessTab = lazyPart(() => import("@/features/admin/users-access").then((m) => m.PendingAccessTab));

const USER_TABS = [{ id: "members", label: "Members" }, { id: "invitations", label: "Invitations" }, { id: "pending", label: "Pending access" }] as const;
type UserTab = (typeof USER_TABS)[number]["id"];
/** What the invite dialog starts with: empty, or an email and name from a pending access request ("Invite instead").
 *  `returnTo` is where focus goes when it closes, if the button that opened it is gone (the pending row's Approve, since
 *  "Invite instead" was in the approve dialog that closed as this one opened). */
export type InvitePrefill = { email: string; name: string; returnTo?: HTMLElement | null };

export function UsersAndRoles() {
  // The tab is in the URL (?tab=invitations); each tab keeps its own filters there too.
  const [params, setParams] = useSearchParams();
  const tab: UserTab = USER_TABS.find((t) => t.id === params.get("tab"))?.id ?? "members";
  const setTab = (t: UserTab) => setParams(t === "members" ? {} : { tab: t });
  const invalidate = useInvalidate();
  const [inviting, setInvitingState] = useState<InvitePrefill | null>(null);
  // Read when the dialog has closed, after `inviting` is null again.
  const inviteReturn = useRef<HTMLElement | null>(null);
  const setInviting = (p: InvitePrefill | null) => { if (p) inviteReturn.current = p.returnTo ?? null; setInvitingState(p); };
  // The invitation just created, with its link: in memory only while the dialog shows it.
  const [invited, setInvited] = useState<Schemas["InvitationCreated"] | null>(null);
  const [notice, setNotice] = useState("");
  const finishInvite = () => {
    if (invited) setNotice(invited.emailed ? `${invited.email} was invited and emailed the link. They join when they open it and accept.` : `${invited.email} was invited. They join when they open the link and accept.`);
    setInvited(null);
    setInviting(null);
  };
  return (
    <>
      <PageHead title="Users & roles" desc="Only IT staff have accounts. Admins manage them here.">
        <Button onClick={() => setInviting({ email: "", name: "" })}>Invite user</Button>
      </PageHead>
      <SuccessNote text={notice} />
      <TabStrip tabs={[...USER_TABS]} tab={tab} onSelect={setTab} label="Users & roles" />
      <div role="tabpanel" id="tabpanel" aria-labelledby={`tab-${tab}`}>
        {tab === "members" && <MembersTab />}
        {tab === "invitations" && <LazyPart what="invitations"><InvitationsTab onInvite={() => setInviting({ email: "", name: "" })} /></LazyPart>}
        {tab === "pending" && <LazyPart what="pending access"><PendingAccessTab onInvite={(p) => setInviting(p)} /></LazyPart>}
      </div>
      {/* One dialog: the form, then the link in its place, so closing it returns focus to the button that opened it. */}
      <Modal title={invited ? (invited.emailed ? "Invitation emailed" : "Copy the invitation link") : "Invite user"} open={inviting !== null} keepOpenOnOutside={invited !== null}
        onClose={finishInvite} returnFocusFallback={() => inviteReturn.current}
        description={invited ? inviteLinkDescription(invited) : "They get a link to join this organization. Nobody is added until they open it and accept."}>
        {invited
          ? <InviteLinkPanel invitation={invited} onDone={finishInvite} />
          // The member isn't added until they accept; the lists are reloaded anyway (another Admin's changes).
          : <InviteForm initial={inviting ?? undefined} onCancel={() => setInviting(null)} onDone={(r) => { setInvited(r); void invalidate("users", "invitations"); }} />}
      </Modal>
    </>
  );
}

type Member = Schemas["Member"];
type Identity = Schemas["UserIdentity"];

function MembersTab() {
  const me = useMe();
  const users = useLoad<Member[]>("users");
  const [temp, setTemp] = useState<{ email: string; password: string } | null>(null);
  const [unlinking, setUnlinking] = useState<{ member: Member; identity: Identity } | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  async function change(u: Member, body: Record<string, unknown>) {
    setError("");
    // One control, one field (a role, or active): the change names itself and sends nothing read earlier.
    setNotice("");
    const failed = await attempt(() => api(`users/${u.id}`, { method: "PATCH", body, headers: ANY_REVISION }));
    if (failed) setError(failed.detail);
    // What deactivating does, and doesn't: sessions end now; API tokens are only refused while inactive.
    else if (body.active === false) setNotice(`${u.name} is deactivated and their sessions have ended. Their API tokens are refused while they are inactive but work again if you reactivate them: if the account was compromised, revoke its tokens in API tokens.`);
    else if (body.active === true) setNotice(`${u.name} is active again. Any API tokens they still have work again.`);
    users.reload();
  }
  async function reset(u: Member) {
    try {
      const r = await api<{ temporary_password: string }>(`users/${u.id}/reset-password`, { method: "POST" });
      setTemp({ email: u.email, password: r.temporary_password });
      users.reload();
    } catch (e) { setError(e instanceof Problem ? e.detail : "Couldn't reset the password."); }
  }
  if (users.error) return <ErrorBanner message={users.error} onRetry={users.reload} />;
  return (
    <>
      <SuccessNote text={notice} />
      {error && <ErrorBanner message={error} />}
      <p className="mb-3 max-w-prose text-sm text-muted-foreground" data-testid="deactivate-help">
        Deactivate ends the member's sessions at once. Their API tokens are refused while they are inactive and work again on
        reactivation, so revoke them in <Link className="underline" to="/admin/tokens">API tokens</Link> for a compromised account.
      </p>
      {!users.data ? <Loading what="users" rows={3} /> : (
        <TableRegion label="Users table">
          <Table>
            <TableCaption className="sr-only">Members of this organization, with their role, status, single sign-on identities and last sign-in</TableCaption>
            <TableHeader><TableRow><TableHead scope="col">User</TableHead><TableHead scope="col">Role</TableHead><TableHead scope="col">Status</TableHead><TableHead scope="col">Single sign-on</TableHead><TableHead scope="col">Last sign-in</TableHead><TableHead scope="col"><span className="sr-only">Actions</span></TableHead></TableRow></TableHeader>
            <TableBody>
              {users.data.map((u) => (
                <TableRow key={u.id} className="align-top">
                  <TableHead scope="row" className="min-w-40 max-w-sm whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">{u.name}<span className="block text-xs font-normal text-muted-foreground">{u.email}</span></TableHead>
                  <TableCell>
                    <label className="sr-only" htmlFor={`role-${u.id}`}>Role for {u.name}</label>
                    <Select id={`role-${u.id}`} className="w-32" value={u.role} onChange={(e) => change(u, { role: e.target.value })}>
                      <option value="admin">Admin</option><option value="editor">Editor</option><option value="viewer">Viewer</option>
                    </Select>
                  </TableCell>
                  <TableCell>
                    {u.active ? (u.must_change_password ? <Chip>Must change password</Chip> : "Active") : <Chip>Deactivated</Chip>}
                  </TableCell>
                  <TableCell className="min-w-56 whitespace-normal"><Identities member={u} onUnlink={(identity) => setUnlinking({ member: u, identity })} /></TableCell>
                  <TableCell>{u.identities === null ? <span className="text-muted-foreground">Not shown for a shared account</span> : u.last_login ? <span className="tabular">{u.last_login.slice(0, 10)}</span> : "Never"}</TableCell>
                  <TableCell className="space-x-2 text-right">
                    {u.id !== me.user.id && <Button size="sm" variant="outline" onClick={() => change(u, { active: !u.active })}>{u.active ? "Deactivate" : "Reactivate"}<span className="sr-only"> {u.name}</span></Button>}
                    {/* An account with no password (made by single sign-on or SCIM) has none to reset; the API refuses it. */}
                    {u.has_password === false
                      ? <span className="inline-block max-w-48 text-left align-middle text-sm text-muted-foreground" data-testid="no-password">No password to reset: signs in through single sign-on</span>
                      : u.id === me.user.id /* The API refuses a reset of your own account (409). */
                        ? <span className="inline-block max-w-48 text-left align-middle text-sm text-muted-foreground" data-testid="own-password">Your account: change your password from your profile</span>
                      : <Button size="sm" variant="outline" onClick={() => reset(u)}>Reset password<span className="sr-only"> for {u.name}</span></Button>}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      <Modal title={unlinking ? `Unlink ${unlinking.identity.provider} from ${unlinking.member.name}?` : ""} open={unlinking !== null} onClose={() => setUnlinking(null)}
        description={unlinking ? `${unlinking.member.name} stops signing in with this ${unlinking.identity.provider} account. Their next sign-in through ${unlinking.identity.provider} matches by email again.` : undefined}>
        {unlinking && <UnlinkForm {...unlinking} onCancel={() => setUnlinking(null)}
          onDone={(text) => { setUnlinking(null); setNotice(text); users.reload(); }} />}
      </Modal>
      <Modal title="Temporary password" open={temp !== null} onClose={() => setTemp(null)}
        description="Share it with the person through a channel you trust. It's shown only once.">
        {temp && (
          <>
            <p className="mb-1 text-sm">{temp.email}</p>
            <code className="mb-4 block rounded-md border bg-muted px-3 py-2 font-mono text-lg">{temp.password}</code>
            <DialogFooter><Button onClick={() => setTemp(null)}>Done</Button></DialogFooter>
          </>
        )}
      </Modal>
    </>
  );
}

/** A member's single sign-on identities: the provider, the issuer and the last sign-in, each with Unlink. Null for an
 *  account that also belongs to another organization: its identities are that account's, not this organization's. */
function Identities({ member, onUnlink }: { member: Member; onUnlink: (identity: Identity) => void }) {
  if (member.identities === null) return <GapChip label="Managed in another organization" />;
  // No identity and no password (an identity unlinked from an account single sign-on made): nothing to sign in with.
  if (member.identities.length === 0) return member.has_password === false ? <GapChip label="No way to sign in" /> : <span className="text-muted-foreground">Password only</span>;
  return (
    <ul className="m-0 flex list-none flex-col gap-2 p-0">
      {member.identities.map((i) => (
        <li key={i.id} className="flex flex-wrap items-start gap-x-2 gap-y-1">
          <span className="min-w-0 flex-1">
            <span className="block">Signs in with {i.provider}</span>
            <span className="block text-xs text-muted-foreground [overflow-wrap:anywhere]">{i.issuer}</span>
            <span className="block text-xs text-muted-foreground">Last sign-in {i.last_login_at ? <span className="tabular">{when(i.last_login_at)}</span> : "never"}</span>
          </span>
          <Button size="sm" variant="outline" onClick={() => onUnlink(i)}>Unlink {i.provider}<span className="sr-only"> for {member.name}</span></Button>
        </li>
      ))}
    </ul>
  );
}

function UnlinkForm({ member, identity, onCancel, onDone }: { member: Member; identity: Identity; onCancel: () => void; onDone: (text: string) => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await typedApi("delete", "users/{id}/identities/{identity_id}", { path: { id: member.id, identity_id: identity.id } });
      onDone(`${member.name} no longer signs in with this ${identity.provider} account.`);
    } catch (err) {
      setError(problemText(err, "Couldn't unlink the identity."));
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit} noValidate>
      {error && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{error}</p>}
      <p className="mb-4 text-sm [overflow-wrap:anywhere]">Issuer: {identity.issuer}</p>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" variant="destructive" disabled={busy}>{busy ? "Unlinking…" : `Unlink ${identity.provider}`}</Button>
      </DialogFooter>
    </form>
  );
}

function InviteForm({ initial, onCancel, onDone }: { initial?: InvitePrefill; onCancel: () => void; onDone: (invitation: Schemas["InvitationCreated"]) => void }) {
  const [form, setForm] = useState<{ email: string; name: string; role: Schemas["RoleEnum"] }>({ email: initial?.email ?? "", name: initial?.name ?? "", role: "editor" });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [saving, setSaving] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (saving) return;
    setSaving(true);
    try {
      onDone(await typedApi("post", "invitations", { body: form }));
    } catch (err) {
      setErrors(err instanceof Problem && Object.keys(err.errors).length ? err.errors : { email: [err instanceof Problem ? err.detail : "Couldn't invite. Try again."] });
    } finally {
      setSaving(false);
    }
  }
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      <Field id="email" label="Email (required)" errors={errors}><Input id="email" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></Field>
      <Field id="name" label="Name" errors={errors}><Input id="name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
      <Field id="role" label="Role" errors={errors}>
        <Select id="role" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Schemas["RoleEnum"] })}>
          <option value="admin">Admin</option><option value="editor">Editor</option><option value="viewer">Viewer</option>
        </Select>
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={saving}>{saving ? "Inviting…" : "Invite"}</Button>
      </DialogFooter>
    </form>
  );
}

export const ROLE_NAME: Record<string, string> = { admin: "Admin", editor: "Editor", viewer: "Viewer" };

/** The link dialog's description: emailed (`emailed`), or to be sent by hand. */
export function inviteLinkDescription(invitation: Schemas["InvitationCreated"], resent = false) {
  const old = resent ? " The previous link no longer works." : "";
  return invitation.emailed
    ? `Softwarden emailed the link to ${invitation.email}.${old} You can also copy it below; it won't be shown again.`
    : `It won't be shown again${resent ? ", and the previous link no longer works" : ""}. Send it to the person through a channel you trust.`;
}

/** The invitation link, once (the API keeps only its fingerprint): a read-only box to select or copy, the warning that
 *  it can't be shown again, and when it stops working. The same pattern as a new API token's secret (tokens.tsx). */
export function InviteLinkPanel({ invitation, onDone }: { invitation: Schemas["InvitationCreated"]; onDone: () => void }) {
  const [status, setStatus] = useState("");
  // The form that had focus is gone: start on the warning, so it is read before anything else.
  const note = useRef<HTMLParagraphElement>(null);
  useEffect(() => { note.current?.focus(); }, []);
  async function copy() {
    try {
      await navigator.clipboard.writeText(invitation.link);
      setStatus("Link copied to the clipboard.");
    } catch {
      setStatus("Couldn't copy. Select the link and copy it by hand.");
    }
  }
  return (
    <>
      {invitation.emailed ? (
        <p ref={note} tabIndex={-1} data-testid="invite-note" className="mb-2 rounded-md border border-success-border bg-success-bg px-3 py-2 text-sm text-success-foreground outline-none focus:ring-2 focus:ring-ring" role="note">
          <span aria-hidden="true">✓ </span><span className="font-semibold">Emailed to {invitation.email}.</span> The link below is the same one, shown only once, in case the email doesn't arrive.
        </p>
      ) : (
        <p ref={note} tabIndex={-1} data-testid="invite-note" className="mb-2 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-sm text-state-pending-foreground outline-none focus:ring-2 focus:ring-ring" role="note">
          <span aria-hidden="true">◐ </span><span className="font-semibold">Shown only once.</span> It wasn't emailed: send it yourself. If it's lost, invite {invitation.email} again: the new link replaces this one.
        </p>
      )}
      <p className="mb-2 text-sm" data-testid="invite-summary">
        <span className="font-medium">{invitation.email}</span> joins as {ROLE_NAME[invitation.role] ?? invitation.role} when they open the link and accept.
        {" "}The link expires on <span className="tabular">{when(invitation.expires_at)}</span>.
      </p>
      <code className="mb-2 block select-all break-all rounded-md border bg-muted px-3 py-2 font-mono text-sm" data-testid="invite-link">{invitation.link}</code>
      <p className="mb-4 text-sm font-medium" role="status">{status}</p>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={() => void copy()}>Copy link</Button>
        <Button type="button" onClick={onDone}>Done</Button>
      </DialogFooter>
    </>
  );
}

// ---- Organization settings (Admin) ---------------------------------------------------------

export function OrganizationSettings() {
  // The settings start from a read with the ETag, never the app's cached copy (the record forms keep one for
  // minutes): the form shows exactly the revision its save names. Shared (one query key) with the page's other
  // organization forms (org-extras.tsx).
  const opened = useOpenedRecord<Org>("organization");
  const stewards = useLoad<UserRef[]>("stewards");
  const invalidate = useInvalidate();
  // "Load the latest version" after a 412 starts the form over from the organization read again.
  const [round, setRound] = useState(0);
  if (opened.error) return <ErrorBanner message={problemText(opened.error, "Couldn't load the organization settings.")} onRetry={() => void opened.reload()} />;
  if (stewards.error) return <ErrorBanner message={stewards.error} onRetry={stewards.reload} />;
  if (!opened.data || !stewards.data) return <Loading what="organization settings" />;
  return (
    <OrgForm key={round} org={opened.data} stewards={stewards.data}
      onLoadLatest={async () => { await opened.reload(); void invalidate("organization"); setRound((n) => n + 1); }} />
  );
}

/** What OrgForm saves, to tell a change someone else made from the page's other forms' saves. */
const orgSettingsOf = (o: Org) => JSON.stringify([
  o.name, o.timezone, o.review_interval_days, o.coordinator?.id ?? null, o.renewal_lead_days, o.reminder_thresholds, o.digest_time,
  o.compliance_profile_enabled, o.sso_auto_provision,
]);

function OrgForm({ org, stewards, onLoadLatest }: { org: Org; stewards: UserRef[]; onLoadLatest: () => Promise<void> }) {
  const me = useMe();
  // If-Match: the organization as last known with its ETag (the opened read, or a save's answer on this page).
  // A save sends that ETag only while the settings this form saves are still the ones it started from; a save on the
  // page's other forms moves the ETag without refusing this one.
  const opened = useOpenedRecord<Org>("organization");
  const [base, setBase] = useState(org);
  const [stale, setStale] = useState(false);
  const [form, setForm] = useState({
    name: org.name, timezone: org.timezone, review_interval_days: String(org.review_interval_days),
    coordinator: org.coordinator ? String(org.coordinator.id) : "",
    renewal_lead_days: String(org.renewal_lead_days), reminder_thresholds: org.reminder_thresholds.join(", "), digest_time: org.digest_time,
  });
  const zones = useMemo(() => zoneOptions(org.timezone), [org.timezone]);
  const [compliance, setCompliance] = useState(org.compliance_profile_enabled);
  const [autoProvision, setAutoProvision] = useState(org.sso_auto_provision);
  const sso = useSsoState();
  // The API refuses sign-in settings while platform staff impersonate a member; say so instead of offering them.
  const impersonating = !!me.impersonation;
  const invalidate = useInvalidate();
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [notice, setNotice] = useState("");
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setNotice("");
    const parts = form.reminder_thresholds.split(/[\s,]+/).filter(Boolean);
    if (parts.some((p) => !/^\d+$/.test(p))) return setErrors({ reminder_thresholds: ["Enter whole numbers of days separated by commas, such as 90, 60, 30, 7."] });
    const body: Record<string, unknown> = { ...form, reminder_thresholds: parts.map(Number), renewal_lead_days: Number(form.renewal_lead_days) || form.renewal_lead_days };
    // Only an edition with the compliance profile may send the switch: elsewhere the key alone is refused (403).
    if (org.compliance_profile_available) body.compliance_profile_enabled = compliance;
    // Sent only when changed: the API audits the switch.
    if (autoProvision !== base.sso_auto_provision) body.sso_auto_provision = autoProvision;
    let stale412 = false;
    const failed = await attempt(async () => {
      const now = await opened.latest();
      if (orgSettingsOf(now.data) !== orgSettingsOf(base)) { stale412 = true; return; }
      const saved = await apiWriteWithEtag<Org>("organization", { method: "PATCH", body, headers: ifMatch(now.etag) });
      opened.adopt(saved);
      setBase(saved.data);
    });
    if (stale412 || failed?.status === 412) { setErrors({}); return setStale(true); }
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { name: [failed.detail] });
    setErrors({});
    // Record forms, lists and detail pages read the switch from GET organization.
    void invalidate("organization");
    const said = ["Settings saved."];
    if (org.compliance_profile_available && compliance !== base.compliance_profile_enabled) {
      said.push(`The compliance profile is ${compliance ? "on: records now show the compliance fields" : "off: the compliance fields are hidden and their values kept"}.`);
    }
    if (autoProvision !== base.sso_auto_provision) {
      said.push(autoProvision
        ? "Auto-provision is on: someone who signs in through single sign-on without an account waits in Users & roles, Pending access."
        : "Auto-provision is off: single sign-on refuses anyone without an account.");
    }
    setNotice(said.join(" "));
  }
  const run = org.email.last_run;
  return (
    <>
      <PageHead title="Organization settings" desc="Defaults that apply to every record in this organization." />
      <SuccessNote text={notice} />
      {stale && <StaleBanner what="organization" onLoad={() => void onLoadLatest()} />}
      <Card className="max-w-xl"><CardContent className="pt-6">
        <form onSubmit={submit} noValidate>
          <ErrorSummary errors={errors} />
          <fieldset className="mb-2">
            <legend className="mb-3 font-display text-section font-semibold">General</legend>
            <Field id="name" label="Organization name" errors={errors}><Input id="name" value={form.name} onChange={set("name")} /></Field>
            {/* The same list of zones as first-run setup: typing filters it, so a zone can't be misspelled. */}
            <Field id="timezone" label="Timezone" errors={errors} help="Dates and deadlines are judged in this timezone, for example America/New_York.">
              <Combobox id="timezone" listLabel="Timezones" options={zones} value={form.timezone} onChange={(z) => setForm({ ...form, timezone: z })} />
            </Field>
            <Field id="review_interval_days" label="Review interval (days)" errors={errors} help="Default time until the next review. 7 to 730 days.">
              <Input id="review_interval_days" type="number" min={7} max={730} value={form.review_interval_days} onChange={set("review_interval_days")} />
            </Field>
          </fieldset>
          <fieldset className="mb-2 border-t pt-4">
            <legend className="mb-3 font-display text-section font-semibold">Routing</legend>
            <Field id="coordinator" label="IT coordinator" errors={errors} help="Gets Needs attention items, and digest items, that have no IT steward or follow-up. Must be an Admin or Editor.">
              <Select id="coordinator" value={form.coordinator} onChange={set("coordinator")}>
                <option value="">Choose a person</option>
                {stewards.map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
              </Select>
            </Field>
          </fieldset>
          {org.compliance_profile_available && (
            <fieldset className="mb-2 border-t pt-4">
              <legend className="mb-3 font-display text-section font-semibold">Compliance profile</legend>
              <label className="flex items-start gap-2 text-sm">
                <input type="checkbox" id="compliance_profile_enabled" checked={compliance} onChange={(e) => setCompliance(e.target.checked)}
                  aria-describedby="compliance-help" className="mt-0.5 h-4 w-4 accent-primary" />
                <span><span className="font-medium">Use the compliance profile</span>
                  <span id="compliance-help" className="block text-muted-foreground">
                    Adds GxP relevance, validation status and document, periodic review dates and risk tier to applications, components and AI agents.
                    Turning it off hides those fields and keeps their values.
                  </span>
                </span>
              </label>
              {errors.compliance_profile_enabled && <p className="mt-1 text-sm text-error-foreground">{errors.compliance_profile_enabled.join(" ")}</p>}
            </fieldset>
          )}
          {sso.available && (
            <fieldset className="mb-2 border-t pt-4">
              <legend className="mb-3 font-display text-section font-semibold">Single sign-on</legend>
              <label className="flex items-start gap-2 text-sm">
                <input type="checkbox" id="sso_auto_provision" checked={autoProvision} onChange={(e) => setAutoProvision(e.target.checked)}
                  disabled={impersonating || (!sso.on && !org.sso_auto_provision)} aria-describedby="sso-auto-provision-help" className="mt-0.5 h-4 w-4 accent-primary" />
                <span><span className="font-medium">Let people without an account ask for access</span>
                  <span id="sso-auto-provision-help" className="block text-muted-foreground">
                    When someone signs in through this organization's single sign-on and has no account here, they're added to Pending access in Users & roles instead of being turned away.
                    They get no access until an Admin approves them with a role. Off: they're refused, and you invite people by email.
                    {impersonating
                      ? <span className="mt-1 block font-medium text-foreground" data-testid="sso-auto-provision-why">{IMPERSONATING_SIGN_IN_SETTINGS}</span>
                      : !sso.on && <span className="mt-1 block font-medium text-foreground" data-testid="sso-auto-provision-why">{sso.why}</span>}
                  </span>
                </span>
              </label>
              {errors.sso_auto_provision && <p className="mt-1 text-sm text-error-foreground">{errors.sso_auto_provision.join(" ")}</p>}
            </fieldset>
          )}
          <fieldset className="mb-4 border-t pt-4">
            <legend className="mb-3 font-display text-section font-semibold">Renewals and digest</legend>
            <Field id="renewal_lead_days" label="Default renewal lead time (days)" errors={errors} help="Used for Assumed and Renewal action deadlines. 1 to 365 days.">
              <Input id="renewal_lead_days" type="number" min={1} max={365} value={form.renewal_lead_days} onChange={set("renewal_lead_days")} />
            </Field>
            <Field id="reminder_thresholds" label="Reminder thresholds (days before the deadline)" errors={errors} help="Largest first, separated by commas, such as 90, 60, 30, 7. Each is sent once per cycle.">
              <Input id="reminder_thresholds" inputMode="numeric" value={form.reminder_thresholds} onChange={set("reminder_thresholds")} />
            </Field>
            <Field id="digest_time" label={`Daily digest time (${org.timezone})`} errors={errors}>
              <Input id="digest_time" type="time" value={form.digest_time} onChange={set("digest_time")} className="w-40" />
            </Field>
            <div className="rounded-md border bg-muted/40 px-3 py-2 text-sm">
              <p className="font-medium">Email delivery</p>
              {/* On the hosted service the Admin can't set the server's variables, so the hint names who can. */}
              {!org.email.configured && <p className="text-error-foreground" data-testid="email-unconfigured"><span aria-hidden="true">! </span>{me.deployment === "cloud"
                ? "The hosted email service isn't configured for this organization. Actions stay available in Needs attention. Contact support to have digests sent."
                : "Email isn't configured. Actions stay available in Needs attention. Set SOFTWARDEN_SMTP_HOST and the other SMTP settings to send digests (docs/ENV.md)."}</p>}
              <p>{run ? `Last digest run ${run.for_date}: ${run.sent} sent, ${run.failed} failed${run.pending ? `, ${run.pending} waiting to retry` : ""}${run.skipped ? `, ${run.skipped} not sent (email off)` : ""}.` : "No digest has run yet."}</p>
              <Link to="/admin/digests">View delivery log</Link>
            </div>
          </fieldset>
          <Button type="submit">Save settings</Button>
        </form>
      </CardContent></Card>
      <LazyPart what="attachment and data-quality settings"><OrganizationExtras /></LazyPart>
      <LazyPart what="limits, exports and deletion"><OrganizationLifecycle /></LazyPart>
    </>
  );
}

/** Whether single sign-on signs members in now (these settings, or the installation's legacy Entra settings), for the
 *  auto-provision switch: with it off, the switch has nothing to act on and says why. Only with capability `sso.oidc`. */
function useSsoState() {
  const me = useMe();
  const available = me.capabilities.includes("sso.oidc");
  const q = useApiQuery<Schemas["SsoSettings"]>("sso/settings", { enabled: available });
  const source = q.data?.effective.source;
  // While it loads, or if it can't be read, the switch stays usable: the API decides.
  const on = !q.data || source === "active" || source === "legacy";
  const why = source === "force_local"
    ? "Single sign-on is turned off on this installation (SOFTWARDEN_FORCE_LOCAL_LOGIN), so this can't be turned on."
    : "Single sign-on isn't active for this organization, so this can't be turned on yet. Activate it in Single sign-on first.";
  return { available, on, why };
}

// ---- Profile (digest preference; saved view emails) -------------------------------------------

const MySubscriptions = lazyPart(() => import("@/features/catalog/saved-views").then((m) => m.MySubscriptions));

export function Profile() {
  const me = useMe();
  const load = useLoad<Preferences>("auth/me/preferences");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  // Shown at once, put back if the save fails.
  const [chosen, setChosen] = useState<Partial<Record<"digest_enabled" | "subscriptions_enabled", boolean>>>({});
  const p = load.data;
  const NOTICE = {
    digest_enabled: ["Daily digest turned on.", "Daily digest turned off. Your items stay in Needs attention."],
    subscriptions_enabled: ["Saved view emails turned on.", "Saved view emails turned off. Your subscriptions are kept; nothing is sent until you turn them back on."],
  } as const;
  async function toggle(key: "digest_enabled" | "subscriptions_enabled", enabled: boolean) {
    setChosen((c) => ({ ...c, [key]: enabled }));
    setSaving(true);
    setNotice("");
    const failed = await attempt(() => api("auth/me/preferences", { method: "PATCH", body: { [key]: enabled } }));
    setSaving(false);
    if (failed) { setChosen((c) => ({ ...c, [key]: !enabled })); return setError(failed.detail); }
    setError("");
    setNotice(NOTICE[key][enabled ? 0 : 1]);
    load.reload();
  }
  const digestOn = chosen.digest_enabled ?? p?.digest_enabled ?? true;
  const viewsOn = chosen.subscriptions_enabled ?? p?.subscriptions_enabled ?? true;
  return (
    <>
      <PageHead title="Your profile" desc={`${me.user.name} · ${me.user.email}`} />
      <SuccessNote text={notice} />
      {error && <ErrorBanner message={error} />}
      {load.error && <ErrorBanner message={load.error} onRetry={load.reload} />}
      {!p && !load.error && <Loading what="your notifications" />}
      {p && (
        <Card className="max-w-xl"><CardContent className="pt-6">
          <h2 className="mb-2 font-display text-section font-semibold">Notifications</h2>
          <label className="flex items-start gap-3">
            <input type="checkbox" className="mt-1 h-4 w-4 accent-primary" checked={digestOn} aria-busy={saving || undefined} onChange={(e) => void toggle("digest_enabled", e.target.checked)} aria-describedby="digest-help" />
            <span>Email me a daily digest</span>
          </label>
          <p id="digest-help" className="mt-1 text-sm text-muted-foreground">
            One email a day at {p.digest_time} ({p.timezone}) listing the Needs attention items assigned to you, when there are any.
            {digestOn ? ` The next one is due ${p.next_digest_at.slice(0, 16).replace("T", " ")}.` : ""} Snoozed items are left out.
          </p>
          <label className="mt-4 flex items-start gap-3">
            <input type="checkbox" className="mt-1 h-4 w-4 accent-primary" checked={viewsOn} aria-busy={saving || undefined} onChange={(e) => void toggle("subscriptions_enabled", e.target.checked)} aria-describedby="views-help" />
            <span>Saved view emails</span>
          </label>
          <p id="views-help" className="mt-1 text-sm text-muted-foreground">
            The weekly emails of the saved views you subscribe to, each on its own day at {p.digest_time > "23:00" ? "23:00" : p.digest_time} ({p.timezone}).
            {viewsOn ? "" : " Turned off: your subscriptions are kept, but nothing is sent."}
          </p>
          <LazyPart what="your weekly emails"><MySubscriptions /></LazyPart>
        </CardContent></Card>
      )}
      {/* A display setting kept in this browser, so it shows whatever the notifications request does (WCAG 2.1.4). */}
      <Card className="mt-4 max-w-xl"><CardContent className="pt-6">
        <h2 className="mb-2 font-display text-section font-semibold">Keyboard</h2>
        <SingleKeySwitch me={me} />
      </CardContent></Card>
    </>
  );
}

// ---- Delivery log --------------------------------------------------------------------------

const DIGEST_STATUS: Record<DigestStatus, string> = { pending: "Waiting to retry", sent: "Sent", failed: "Failed", skipped: "Not sent: email off" };

export function DeliveryLog() {
  const [params, setParams] = useSearchParams();
  const status = (params.get("status") ?? "") as DigestStatus | "";
  const load = useLoad<DigestRow[]>(`admin/digests${status ? `?status=${status}` : ""}`);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  async function retry(d: DigestRow) {
    setBusy(d.id);
    setNotice("");
    try {
      const after = await api<DigestRow>(`admin/digests/${d.id}/retry`, { method: "POST" });
      setError("");
      setNotice(after.status === "sent" ? `Digest for ${after.user.name} sent.` : `Digest for ${after.user.name} failed again: ${after.last_error}`);
      load.reload();
    } catch (e) {
      setError(e instanceof Problem ? e.detail : "Couldn't retry the digest.");
    } finally {
      setBusy("");
    }
  }
  const rows = load.data;
  return (
    <>
      <Breadcrumb trail={[{ to: "/admin/organization", label: "Organization settings" }]} current="Delivery log" />
      <PageHead title="Digest delivery log" desc="Each daily digest, who it was for and whether it was sent. Failures are retried after about 1, 4 and 16 minutes, and each digest is also in the notification log. Every item stays in Needs attention whatever happens here." />
      <div className="mb-4 flex flex-col gap-1.5">
        <Label htmlFor="digest-status">Status</Label>
        <Select id="digest-status" className="w-56" value={status} onChange={(e) => setParams(e.target.value ? { status: e.target.value } : {})}>
          <option value="">All</option>
          {(Object.keys(DIGEST_STATUS) as DigestStatus[]).map((s) => <option key={s} value={s}>{DIGEST_STATUS[s]}</option>)}
        </Select>
      </div>
      <SuccessNote text={notice} />
      {error && <ErrorBanner message={error} />}
      {load.error && <ErrorBanner message={load.error} onRetry={load.reload} />}
      {!rows && !load.error && <Loading what="the delivery log" rows={3} />}
      {rows && rows.length === 0 && <EmptyState title={status ? "No digests with this status" : "No digests yet"}>Digests appear here after the first daily run. Nobody gets an email on a day with nothing to send.</EmptyState>}
      {rows && rows.length > 0 && (
        <TableRegion label="Delivery log table">
          <Table>
            <TableCaption className="sr-only">Digests, newest first</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Date</TableHead><TableHead scope="col">User</TableHead><TableHead scope="col">Status</TableHead>
              <TableHead scope="col">Items</TableHead><TableHead scope="col">Attempts</TableHead><TableHead scope="col">Last error</TableHead>
              <TableHead scope="col"><span className="sr-only">Action</span></TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((d) => (
                <TableRow key={d.id} className="align-top">
                  <TableCell className="tabular">{d.for_date}</TableCell>
                  <TableHead scope="row" className="font-normal text-foreground">{d.user.name}</TableHead>
                  <TableCell>
                    {d.status === "failed" ? <span className="font-semibold text-error-foreground"><span aria-hidden="true">! </span>Failed</span> : DIGEST_STATUS[d.status]}
                    {d.next_attempt_at && <span className="block text-sm text-muted-foreground">Next try {d.next_attempt_at.slice(11, 16)} UTC</span>}
                  </TableCell>
                  <TableCell className="tabular">{d.item_count}</TableCell>
                  <TableCell className="tabular">{d.attempts}</TableCell>
                  <TableCell className="max-w-xs whitespace-normal text-muted-foreground [overflow-wrap:anywhere]">{d.last_error || "—"}</TableCell>
                  <TableCell className="text-right">
                    {d.status !== "sent" && d.status !== "skipped" && (
                      <Button size="sm" variant="outline" disabled={busy === d.id} onClick={() => void retry(d)}>
                        {busy === d.id ? "Sending…" : "Retry now"}<span className="sr-only"> for {d.user.name}, {d.for_date}</span>
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
    </>
  );
}
