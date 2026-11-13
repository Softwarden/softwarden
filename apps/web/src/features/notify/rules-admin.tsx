/** Administration › Notification rules: the list with the built-ins marked, the add and edit form,
 *  the preview of what a rule would send, and a test send. Admin only, reads included (the API refuses other roles:
 *  a rule names who is emailed and where), so other roles who open a link see AdminOnly. Loaded on first use. */
import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, apiWithEtag, PRECONDITION_FAILED, Problem, type NamedRef, type Person } from "@/api";
import { AdminOnly, Breadcrumb, EmptyState, ErrorBanner, ErrorSummary, Field, isNotFound, Loading, NotFound, PageHead, Select, TableRegion, TabStrip, useReturnFocus } from "@/components/app";
import { CheckboxPicker } from "@/components/checkbox-picker";
import { chip, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { apiKey, problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { useMe } from "@/lib/session";
import {
  CRITICALITY_CHOICES, leadText, OnOffChip, RECORD_KIND_CHOICES, ROLES, sourceLabel, SOURCE_TYPES, stageText, stepText,
  TestResultList, type Destination, type NotificationRule, type RulePreview, type RuleTestSendResult,
} from "@/features/notify/notify-ui";
import { useResetOn } from "@/features/notify/notify";

const WHAT = "the notification rules: which reminders are sent, how far ahead, to whom and through which channels";
const LIST = "notification-rules";

function BuiltinChip() {
  return <span className={chip({ state: "neutral" })}><span aria-hidden="true">◆</span>Built-in</span>;
}

/** Who a rule's email reaches, in words. */
function audienceText(r: NotificationRule) {
  const parts: string[] = [];
  if (r.notify_routed) parts.push("Routed owner");
  if (r.people.length) parts.push(r.people.map((p) => p.name).join(", "));
  if (r.team) parts.push(`Team ${r.team.name}`);
  if (r.roles.length) parts.push(r.roles.map((x) => ROLES.find((y) => y.value === x)?.label ?? x).join(", "));
  return parts.join("; ");
}

function channelsText(r: NotificationRule) {
  const parts: string[] = [];
  if (r.send_email) parts.push("Email");
  if (r.teams_destinations.length) parts.push(`Teams: ${r.teams_destinations.map((d) => d.name + (d.enabled ? "" : " (off)")).join(", ")}`);
  if (r.webhook_destinations.length) parts.push(`Webhook: ${r.webhook_destinations.map((d) => d.name + (d.enabled ? "" : " (off)")).join(", ")}`);
  return parts.join("; ");
}

// ---- the list ---------------------------------------------------------------------------------------------------

export function NotificationRulesPage() {
  return <AdminOnly title="Notification rules" what={WHAT}><RulesList /></AdminOnly>;
}

function RulesList() {
  const q = useApiQuery<NotificationRule[]>(LIST);
  const rules = q.data;
  return (
    <>
      <PageHead title="Notification rules" desc="Which reminders are sent, how far ahead, to whom and where. The built-in rules come with every organization: change them or turn them off; they can't be deleted.">
        <Button asChild><Link to="/admin/notification-rules/new">Add rule</Link></Button>
      </PageHead>
      <p className="mb-4 max-w-prose text-sm text-muted-foreground">
        Set up the email transport, Teams channels and webhooks in <Link className="underline" to="/admin/notification-channels">Notification channels</Link>; see what was sent in the <Link className="underline" to="/admin/notification-log">Notification log</Link>.
      </p>
      {q.isError && <ErrorBanner message={problemText(q.error, "Couldn't load the notification rules.")} onRetry={() => void q.refetch()} />}
      {!rules && !q.isError && <Loading what="notification rules" rows={4} />}
      {rules && rules.length === 0 && (
        <EmptyState title="No notification rules" action={<Button asChild variant="outline"><Link to="/admin/notification-rules/new">Add rule</Link></Button>}>
          Rules say which reminders are sent and to whom. Without one, nobody is told about upcoming deadlines; Needs attention still lists them.
        </EmptyState>
      )}
      {rules && rules.length > 0 && (
        <TableRegion label="Notification rules table">
          <Table>
            <TableCaption className="sr-only">Notification rules, by name</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Rule</TableHead><TableHead scope="col">Status</TableHead><TableHead scope="col">Reminders</TableHead>
              <TableHead scope="col">When</TableHead><TableHead scope="col">Channels</TableHead><TableHead scope="col">Email goes to</TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {rules.map((r) => (
                <TableRow key={r.id} className="align-top">
                  <TableHead scope="row" className="font-semibold text-foreground">
                    <span className="inline-flex flex-wrap items-center gap-2">
                      <Link to={`/admin/notification-rules/${r.id}`} className="underline">{r.name}</Link>
                      {r.builtin && <BuiltinChip />}
                    </span>
                  </TableHead>
                  <TableCell><OnOffChip on={r.enabled} /></TableCell>
                  <TableCell className="max-w-xs whitespace-normal">{r.kind === "digest" ? "Everyone's Needs attention items, once a day" : r.source_types.map(sourceLabel).join(", ")}</TableCell>
                  <TableCell className="max-w-xs whitespace-normal">
                    {r.kind === "digest" ? `Daily at ${r.digest_time}` : <>
                      {leadText(r.lead_times_days) || "No advance notices"}
                      {r.escalation_days.length > 0 && <span className="block text-sm text-muted-foreground">Escalates {stepText(r.escalation_days)}</span>}
                      {r.repeat_every_days != null && <span className="block text-sm text-muted-foreground">Repeats every {r.repeat_every_days} {r.repeat_every_days === 1 ? "day" : "days"} after the due date</span>}
                    </>}
                  </TableCell>
                  <TableCell className="max-w-xs whitespace-normal">{r.kind === "digest" ? "Email" : channelsText(r)}</TableCell>
                  <TableCell className="max-w-xs whitespace-normal">{r.kind === "digest" ? "Each member with items, who hasn't turned the digest off" : (r.send_email ? audienceText(r) || "—" : "—")}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
    </>
  );
}

// ---- add and edit ---------------------------------------------------------------------------------------------------

export function NewNotificationRulePage() {
  return <AdminOnly title="Add notification rule" what={WHAT}><RuleEditor id={null} /></AdminOnly>;
}

export function NotificationRulePage() {
  const { id } = useParams();
  return <AdminOnly title="Notification rule" what={WHAT}><RuleEditor id={id ?? null} /></AdminOnly>;
}

type Tab = "settings" | "preview" | "test";
const TABS: { id: Tab; label: string }[] = [{ id: "settings", label: "Settings" }, { id: "preview", label: "Preview" }, { id: "test", label: "Test send" }];

function RuleEditor({ id }: { id: string | null }) {
  const path = id ? `${LIST}/${id}` : "";
  const rule = useQuery({ queryKey: [...apiKey(path), "etag"], queryFn: () => apiWithEtag<NotificationRule>(path), enabled: !!id, gcTime: 0 });
  const [params, setParams] = useSearchParams();
  const asked = params.get("tab") as Tab | null;
  const tab: Tab = asked && TABS.some((t) => t.id === asked) ? asked : "settings";
  const setTab = (t: Tab) => setParams((p) => { const n = new URLSearchParams(p); if (t === "settings") n.delete("tab"); else n.set("tab", t); return n; });
  const location = useLocation();
  const [notice, setNotice] = useState(() => (location.state as { notice?: string } | null)?.notice ?? "");
  // The "added" notice came in the navigation's state: drop it from history once shown, so a reload or Back doesn't
  // announce it again.
  const navigateTo = useNavigate();
  useEffect(() => {
    if ((location.state as { notice?: string } | null)?.notice) navigateTo({ pathname: location.pathname, search: location.search }, { replace: true, state: null });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const [deleting, setDeleting] = useState(false);

  if (id && rule.isError && isNotFound(rule.error)) {
    return <NotFound what="notification rule" back={{ to: "/admin/notification-rules", label: "Notification rules" }} trail={[{ to: "/admin/notification-rules", label: "Notification rules" }]} />;
  }
  if (id && rule.isError) {
    return <>
      <Breadcrumb trail={[{ to: "/admin/notification-rules", label: "Notification rules" }]} current="Rule" />
      <PageHead title="Notification rule" />
      <ErrorBanner message={problemText(rule.error, "Couldn't load the rule.")} onRetry={() => void rule.refetch()} />
    </>;
  }
  if (id && !rule.data) return <Loading what="the rule" />;
  const r = rule.data?.data ?? null;
  return (
    <>
      <Breadcrumb trail={[{ to: "/admin/notification-rules", label: "Notification rules" }]} current={r ? r.name : "Add rule"} />
      <PageHead title={r ? r.name : "Add notification rule"} docTitle={r ? `${r.name} · Notification rules` : "Add notification rule"}
        desc={r ? <span className="inline-flex flex-wrap items-center gap-2"><OnOffChip on={r.enabled} />{r.builtin && <BuiltinChip />}{r.kind === "digest" ? "The daily digest of everyone's Needs attention items." : "A reminders rule."}</span>
          : "Which reminders, how far ahead, through which channels and to whom. It starts quietly: notices already due when it is saved are recorded in the log as baselines, not sent."}>
        {r && !r.builtin && <Button variant="outline" onClick={() => setDeleting(true)}>Delete rule</Button>}
      </PageHead>
      <SuccessNote text={notice} />
      {r && <TabStrip tabs={TABS} tab={tab} onSelect={setTab} label="Rule" />}
      <div role={r ? "tabpanel" : undefined} id={r ? "tabpanel" : undefined} aria-labelledby={r ? `tab-${tab}` : undefined}>
        {(!r || tab === "settings") && <RuleForm key={rule.data?.etag ?? "new"} rule={r} etag={rule.data?.etag ?? null} onSaved={(text) => { setNotice(text); void rule.refetch(); }} onStale={() => void rule.refetch()} />}
        {r && tab === "preview" && <RulePreviewPanel rule={r} />}
        {r && tab === "test" && <RuleTestSend rule={r} />}
      </div>
      {r && <DeleteRuleDialog rule={r} etag={rule.data?.etag ?? null} open={deleting} onClose={() => setDeleting(false)} />}
    </>
  );
}

type Form = {
  name: string; enabled: boolean; source_types: string[]; lead_times_days: string; escalation_days: string; repeat_every_days: string; escalate_when_acknowledged: boolean;
  send_email: boolean; teams_destinations: string[]; webhook_destinations: string[];
  notify_routed: boolean; people: string[]; team: string; roles: string[];
  escalation_people: string[]; escalation_team: string; escalation_roles: string[];
  fallback_team: string; fallback_person: string;
  criticalities: string[]; record_kinds: string[]; vendor: string; min_annual_value: string; digest_time: string;
};

const ids = (rows: { id: string }[]) => rows.map((x) => x.id);
function formOf(r: NotificationRule | null): Form {
  return {
    name: r?.name ?? "", enabled: r?.enabled ?? true, source_types: r?.source_types ?? [],
    lead_times_days: (r?.lead_times_days ?? [30, 7]).join(", "), escalation_days: (r?.escalation_days ?? []).join(", "),
    repeat_every_days: r?.repeat_every_days != null ? String(r.repeat_every_days) : "",
    escalate_when_acknowledged: r?.escalate_when_acknowledged ?? false,
    send_email: r?.send_email ?? true, teams_destinations: ids(r?.teams_destinations ?? []), webhook_destinations: ids(r?.webhook_destinations ?? []),
    notify_routed: r?.notify_routed ?? true, people: ids(r?.people ?? []), team: r?.team?.id ?? "", roles: r?.roles ?? [],
    escalation_people: ids(r?.escalation_people ?? []), escalation_team: r?.escalation_team?.id ?? "", escalation_roles: r?.escalation_roles ?? [],
    fallback_team: r?.fallback_team?.id ?? "", fallback_person: r?.fallback_person?.id ?? "",
    criticalities: r?.criticalities ?? [], record_kinds: r?.record_kinds ?? [], vendor: r?.vendor?.id ?? "",
    min_annual_value: r?.min_annual_value ?? "", digest_time: r?.digest_time ?? "",
  };
}

/** "30, 7" as [30, 7]; null when a part isn't a whole number. */
function days(text: string): number[] | null {
  const parts = text.split(/[\s,]+/).filter(Boolean);
  return parts.every((p) => /^\d+$/.test(p)) ? parts.map(Number) : null;
}

/** The request body for the form (every field the rule's kind takes), or client errors. */
function payloadOf(f: Form, digest: boolean): { body: Record<string, unknown>; errors: Record<string, string[]> } {
  if (digest) return { body: { name: f.name.trim(), enabled: f.enabled, digest_time: f.digest_time }, errors: {} };
  const errors: Record<string, string[]> = {};
  const leads = days(f.lead_times_days);
  const steps = days(f.escalation_days);
  if (!leads) errors.lead_times_days = ["Enter whole numbers of days separated by commas, such as 30, 7 (0 is the due date)."];
  if (!steps) errors.escalation_days = ["Enter whole numbers of days separated by commas, such as 1, 7."];
  const repeatText = f.repeat_every_days.trim();
  const repeat = repeatText === "" ? null : /^\d+$/.test(repeatText) ? Number(repeatText) : NaN;
  if (Number.isNaN(repeat)) errors.repeat_every_days = ["Enter a whole number of days, such as 30, or leave it empty for no repeat."];
  const body = {
    name: f.name.trim(), enabled: f.enabled, source_types: f.source_types, lead_times_days: leads ?? [], escalation_days: steps ?? [],
    repeat_every_days: Number.isNaN(repeat) ? null : repeat,
    escalate_when_acknowledged: f.escalate_when_acknowledged, send_email: f.send_email,
    teams_destinations: f.teams_destinations, webhook_destinations: f.webhook_destinations,
    notify_routed: f.notify_routed, people: f.people, team: f.team || null, roles: f.roles,
    escalation_people: f.escalation_people, escalation_team: f.escalation_team || null, escalation_roles: f.escalation_roles,
    fallback_team: f.fallback_team || null, fallback_person: f.fallback_person || null,
    criticalities: f.criticalities, record_kinds: f.record_kinds, vendor: f.vendor || null,
    min_annual_value: f.min_annual_value.trim() || null,
  };
  return { body, errors };
}

function Check({ id, label, help, checked, onChange }: { id: string; label: string; help?: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="mb-3">
      <label className="flex items-start gap-2 text-sm" htmlFor={id}>
        <input type="checkbox" id={id} checked={checked} onChange={(e) => onChange(e.target.checked)} className="mt-0.5 h-4 w-4 accent-primary"
          aria-describedby={help ? `${id}-help` : undefined} />
        <span><span className="font-medium">{label}</span>{help && <span id={`${id}-help`} className="block text-muted-foreground">{help}</span>}</span>
      </label>
    </div>
  );
}

function RuleForm({ rule, etag, onSaved, onStale }: { rule: NotificationRule | null; etag: string | null; onSaved: (text: string) => void; onStale: () => void }) {
  const digest = rule?.kind === "digest";
  const timeZone = useMe().organization.timezone;
  const people = useApiQuery<Person[]>("people", { enabled: !digest });
  const teams = useApiQuery<NamedRef[]>("teams", { enabled: !digest });
  const vendors = useApiQuery<NamedRef[]>("vendors", { enabled: !digest });
  const teamsDest = useApiQuery<Destination[]>("notification-settings/teams-destinations", { enabled: !digest });
  const hooks = useApiQuery<Destination[]>("notification-settings/webhook-destinations", { enabled: !digest });
  const [form, setForm] = useState<Form>(() => formOf(rule));
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [stale, setStale] = useState(false);
  const [saving, setSaving] = useState(false);
  const invalidate = useInvalidate();
  const navigate = useNavigate();
  const set = <K extends keyof Form>(k: K) => (v: Form[K]) => setForm((f) => ({ ...f, [k]: v }));
  const text = (k: keyof Form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const lists = [people, teams, vendors, teamsDest, hooks];
  if (!digest && lists.some((l) => l.isError)) {
    return <ErrorBanner message="Couldn't load the people, teams, vendors and destinations this form offers." onRetry={() => lists.forEach((l) => void l.refetch())} />;
  }
  if (!digest && lists.some((l) => !l.data)) return <Loading what="the rule's choices" />;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const { body, errors: local } = payloadOf(form, digest);
    if (Object.keys(local).length) return setErrors(local);
    let send = body;
    if (rule) {
      const before = payloadOf(formOf(rule), digest).body;
      send = Object.fromEntries(Object.entries(body).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(before[k])));
      if (!Object.keys(send).length) { setErrors({}); return onSaved("Nothing changed."); }
    }
    setSaving(true);
    try {
      if (rule) {
        await api(`${LIST}/${rule.id}`, { method: "PATCH", body: send, headers: etag ? { "If-Match": etag } : {} });
        setErrors({});
        void invalidate(LIST);
        // The API baselines when a change can widen what the rule sends (anything but the name) while it is on.
        const widens = Object.keys(send).some((k) => k !== "name");
        onSaved(`Rule saved.${form.enabled && !digest && widens ? " Notices the change makes due today or earlier are recorded as baselines, not sent." : ""}`);
      } else {
        const made = await api<NotificationRule>(LIST, { method: "POST", body: send });
        void invalidate(LIST);
        navigate(`/admin/notification-rules/${made.id}`, { state: { notice: `Rule ${made.name} added.${made.enabled ? " Notices already due were recorded as baselines in the log, not sent." : ""}` } });
      }
    } catch (err) {
      if (err instanceof Problem && err.status === 412 && err.type === PRECONDITION_FAILED) setStale(true);
      else setErrors(err instanceof Problem && Object.keys(err.errors).length ? err.errors : { name: [problemText(err, "Couldn't save the rule.")] });
    } finally {
      setSaving(false);
    }
  }

  const active = (people.data ?? []).filter((p) => p.active || form.people.includes(p.id) || form.escalation_people.includes(p.id) || form.fallback_person === p.id);
  const personOptions = active.map((p) => ({ id: p.id, label: p.name, hint: p.active ? p.email : "inactive" }));
  const teamOptions = (teams.data ?? []).map((t) => ({ id: t.id, label: t.name }));
  const destOptions = (rows: Destination[] | undefined) => (rows ?? []).map((d) => ({ id: d.id, label: d.name, hint: d.enabled ? d.url_host : `off · ${d.url_host}` }));
  const roleOptions = ROLES.map((r) => ({ id: r.value, label: r.label }));
  const teamSelect = (id: keyof Form, label: string, help?: string) => (
    <Field id={id} label={label} errors={errors} help={help}>
      <Select id={id} value={form[id] as string} onChange={text(id)}>
        <option value="">None</option>
        {teamOptions.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
      </Select>
    </Field>
  );
  return (
    <Card className="max-w-3xl"><CardContent className="pt-6">
      {stale && (
        <div className="mb-4 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground" role="alert">
          <p className="font-semibold">Someone changed this rule since you opened it. Nothing was saved.</p>
          <div className="mt-2"><Button size="sm" variant="outline" onClick={onStale}>Load the latest version</Button></div>
        </div>
      )}
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={errors} focusOnShow="field" />
        <fieldset className="mb-2">
          <legend className="mb-3 font-display text-section font-semibold">Rule</legend>
          <Field id="name" label="Name (required)" errors={errors}><Input id="name" value={form.name} onChange={text("name")} maxLength={200} /></Field>
          <Check id="enabled" label="Rule is on" checked={form.enabled} onChange={set("enabled")}
            help={digest ? "Off: nobody gets the daily digest. Each person can also turn it off in their profile." : "Off: the rule sends nothing. Turning it on again doesn't send what was missed."} />
          {digest && (
            <Field id="digest_time" label={`Send at (${timeZone})`} errors={errors} help="The organization's digest time; also in Organization settings.">
              <Input id="digest_time" type="time" value={form.digest_time} onChange={text("digest_time")} className="w-40" />
            </Field>
          )}
        </fieldset>
        {!digest && <>
          <fieldset className="mb-2 border-t pt-4">
            <legend className="mb-3 font-display text-section font-semibold">Reminders</legend>
            <CheckboxPicker id="source_types" legend="Reminder types (at least one)" options={SOURCE_TYPES.map((s) => ({ id: s.value, label: s.label }))}
              value={form.source_types} onChange={set("source_types")} errors={errors.source_types} />
            <Field id="lead_times_days" label="Lead times (days before the due date)" errors={errors}
              help="Largest first, separated by commas, such as 30, 7. 0 is the due date itself. Up to 10, each 0 to 365; each is sent once.">
              <Input id="lead_times_days" inputMode="numeric" value={form.lead_times_days} onChange={text("lead_times_days")} />
            </Field>
            <Field id="escalation_days" label="Escalation steps (days after the due date)" errors={errors}
              help="Sent while the reminder is still open, such as 1, 7. Leave empty for no escalation. Up to 10, each 1 to 365.">
              <Input id="escalation_days" inputMode="numeric" value={form.escalation_days} onChange={text("escalation_days")} />
            </Field>
            <Field id="repeat_every_days" label="Repeat every (days)" errors={errors}
              help="After the due date, send again every this many days while the reminder is still open, such as 30. Leave empty for no repeat. 1 to 365.">
              <Input id="repeat_every_days" inputMode="numeric" value={form.repeat_every_days} onChange={text("repeat_every_days")} />
            </Field>
            <Check id="escalate_when_acknowledged" label="Escalate even when acknowledged" checked={form.escalate_when_acknowledged} onChange={set("escalate_when_acknowledged")}
              help="Acknowledging a reminder stops its advance notices. With this on, the escalation steps and repeats still go out until it is resolved; with it off, acknowledging stops them too." />
          </fieldset>
          <fieldset className="mb-2 border-t pt-4">
            <legend className="mb-3 font-display text-section font-semibold">Channels</legend>
            <Check id="send_email" label="Send email" checked={form.send_email} onChange={set("send_email")}
              help="To the people chosen under Who gets the email." />
            {errors.send_email && <p className="-mt-2 mb-3 text-sm text-error-foreground">{errors.send_email.join(" ")}</p>}
            <CheckboxPicker id="teams_destinations" legend="Teams channels" options={destOptions(teamsDest.data)} value={form.teams_destinations} onChange={set("teams_destinations")}
              errors={errors.teams_destinations} empty={<p className="text-sm text-muted-foreground">No Teams channels yet. <Link className="underline" to="/admin/notification-channels?tab=teams">Add one in Notification channels</Link>.</p>}
              help="A channel that is off is left out when the rule sends." />
            <CheckboxPicker id="webhook_destinations" legend="Webhooks" options={destOptions(hooks.data)} value={form.webhook_destinations} onChange={set("webhook_destinations")}
              errors={errors.webhook_destinations} empty={<p className="text-sm text-muted-foreground">No webhooks yet. <Link className="underline" to="/admin/notification-channels?tab=webhooks">Add one in Notification channels</Link>.</p>} />
          </fieldset>
          <fieldset className="mb-2 border-t pt-4">
            <legend className="mb-3 font-display text-section font-semibold">Who gets the email</legend>
            <Check id="notify_routed" label="The routed owner" checked={form.notify_routed} onChange={set("notify_routed")}
              help="A record's IT steward, a renewal's IT follow-up and so on, as Needs attention routes the item. When routing finds nobody: the fallback below, else the IT coordinator." />
            {errors.notify_routed && <p className="-mt-2 mb-3 text-sm text-error-foreground" id="notify_routed-error">{errors.notify_routed.join(" ")}</p>}
            <CheckboxPicker id="people" legend="People" options={personOptions} value={form.people} onChange={set("people")} errors={errors.people}
              help="Active people with an email address." />
            {teamSelect("team", "Team", "Every member of the team.")}
            <CheckboxPicker id="roles" legend="Roles" options={roleOptions} value={form.roles} onChange={set("roles")} errors={errors.roles} columns
              help="Every active member of the organization with the role." />
          </fieldset>
          <fieldset className="mb-2 border-t pt-4">
            <legend className="mb-3 font-display text-section font-semibold">Also told on escalation</legend>
            <p className="mb-3 text-sm text-muted-foreground">Added to the recipients above for the escalation steps only.</p>
            <CheckboxPicker id="escalation_people" legend="Escalation people" options={personOptions} value={form.escalation_people} onChange={set("escalation_people")} errors={errors.escalation_people} />
            {teamSelect("escalation_team", "Escalation team")}
            <CheckboxPicker id="escalation_roles" legend="Escalation roles" options={roleOptions} value={form.escalation_roles} onChange={set("escalation_roles")} errors={errors.escalation_roles} columns />
          </fieldset>
          <fieldset className="mb-2 border-t pt-4">
            <legend className="mb-3 font-display text-section font-semibold">When routing finds nobody</legend>
            {teamSelect("fallback_team", "Fallback team", "Stands in for the routed owner. With no fallback, the IT coordinator does.")}
            <Field id="fallback_person" label="Fallback person" errors={errors}>
              <Select id="fallback_person" value={form.fallback_person} onChange={text("fallback_person")}>
                <option value="">None</option>
                {personOptions.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
              </Select>
            </Field>
          </fieldset>
          <fieldset className="mb-4 border-t pt-4">
            <legend className="mb-3 font-display text-section font-semibold">Filters</legend>
            <p className="mb-3 text-sm text-muted-foreground">Leave a filter empty to include everything. With any filter set, only reminders that match all of them are sent.</p>
            <CheckboxPicker id="criticalities" legend="Criticality" options={CRITICALITY_CHOICES.map((c) => ({ id: c.value, label: c.label }))} value={form.criticalities}
              onChange={set("criticalities")} errors={errors.criticalities} columns help="Reminders about a record, or covering one, with one of these criticalities." />
            <CheckboxPicker id="record_kinds" legend="Record kinds" options={RECORD_KIND_CHOICES.map((c) => ({ id: c.value, label: c.label }))} value={form.record_kinds}
              onChange={set("record_kinds")} errors={errors.record_kinds} columns />
            <Field id="vendor" label="Vendor" errors={errors} help="Only renewals, contracts, license pools and records of this vendor.">
              <Select id="vendor" value={form.vendor} onChange={text("vendor")}>
                <option value="">Any vendor</option>
                {(vendors.data ?? []).map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
              </Select>
            </Field>
            <Field id="min_annual_value" label="Minimum annual value" errors={errors} help="Only contracts and renewals worth at least this a year, such as 10000. Leave empty for any.">
              <Input id="min_annual_value" inputMode="decimal" value={form.min_annual_value} onChange={text("min_annual_value")} className="w-48" />
            </Field>
          </fieldset>
        </>}
        <Button type="submit" disabled={saving}>{saving ? "Saving…" : rule ? "Save rule" : "Add rule"}</Button>
      </form>
    </CardContent></Card>
  );
}

function DeleteRuleDialog({ rule, etag, open, onClose }: { rule: NotificationRule; etag: string | null; open: boolean; onClose: () => void }) {
  const returnFocus = useReturnFocus(open);
  const [error, setError] = useResetOn(open, "");
  const [busy, setBusy] = useState(false);
  const invalidate = useInvalidate();
  const navigate = useNavigate();
  async function remove() {
    setBusy(true);
    try {
      // The ETag of the rule as this page read it.
      await api(`${LIST}/${rule.id}`, { method: "DELETE", headers: etag ? { "If-Match": etag } : undefined });
      void invalidate(LIST);
      navigate("/admin/notification-rules");
    } catch (e) {
      setError(e instanceof Problem && e.status === 412
        ? "Someone else changed this rule after you opened it, so it wasn't deleted. Close this and check it, then delete it again if you still want to."
        : problemText(e, "Couldn't delete the rule."));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Delete the rule {rule.name}?</DialogTitle>
          <DialogDescription>It stops sending. What it already sent stays in the notification log under its name. To pause it instead, turn it off.</DialogDescription>
        </DialogHeader>
        {error && <p className="text-sm text-error-foreground" role="alert">{error}</p>}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={busy} onClick={() => void remove()}>{busy ? "Deleting…" : `Delete ${rule.name}`}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---- preview --------------------------------------------------------------------------------------------------------

const PREVIEW_DAYS = [7, 14, 30, 60, 90, 180];

function RulePreviewPanel({ rule }: { rule: NotificationRule }) {
  const [params, setParams] = useSearchParams();
  const asked = Number(params.get("days"));
  const daysAhead = PREVIEW_DAYS.includes(asked) ? asked : 30;
  const q = useApiQuery<RulePreview>(`${LIST}/${rule.id}/preview?days=${daysAhead}`, { enabled: rule.kind === "reminders" });
  if (rule.kind === "digest") {
    return <EmptyState title="The daily digest has no preview">It lists each member's own Needs attention items at the digest time. Use Test send to get your own digest now.</EmptyState>;
  }
  const events = q.data?.events;
  return (
    <section aria-labelledby="preview-heading">
      <h2 id="preview-heading" className="mb-1 font-display text-section font-semibold">What it would send</h2>
      <p className="mb-3 max-w-prose text-sm text-muted-foreground">
        Each notice on the day it would go out if nothing else changed: snoozes end on their date and nobody acknowledges or resolves meanwhile. Only recipients and channels that haven't had it are listed.
        {!rule.enabled && " The rule is off; this shows what it would send if it were on."}
      </p>
      <div className="mb-4 flex flex-col gap-1.5">
        <label htmlFor="preview-days" className="text-sm font-medium">Next</label>
        <Select id="preview-days" className="w-40" value={String(daysAhead)}
          onChange={(e) => setParams((p) => { const n = new URLSearchParams(p); n.set("days", e.target.value); return n; })}>
          {PREVIEW_DAYS.map((d) => <option key={d} value={d}>{d} days</option>)}
        </Select>
      </div>
      {q.isError && <ErrorBanner message={problemText(q.error, "Couldn't load the preview.")} onRetry={() => void q.refetch()} />}
      {!events && !q.isError && <Loading what="the preview" rows={3} />}
      {events && events.length === 0 && <EmptyState title={`Nothing would go out in the next ${daysAhead} days`}>No reminder of the rule's types reaches one of its lead times or escalation steps in this window, or everyone already had it.</EmptyState>}
      {events && events.length > 0 && (
        <TableRegion label="Preview table">
          <Table>
            <TableCaption className="sr-only">Notices the rule would send in the next {daysAhead} days, by date</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Goes out</TableHead><TableHead scope="col">Reminder</TableHead><TableHead scope="col">Due date</TableHead>
              <TableHead scope="col">Stage</TableHead><TableHead scope="col">Email to</TableHead><TableHead scope="col">Channels</TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {events.map((ev, i) => (
                <TableRow key={`${ev.reminder.id}-${ev.date}-${i}`} className="align-top">
                  <TableCell className="tabular">{ev.date}</TableCell>
                  <TableHead scope="row" className="font-normal text-foreground whitespace-normal">
                    <span className="font-semibold">{ev.reminder.title}</span>
                    <span className="block text-sm text-muted-foreground">{ev.reminder.label}</span>
                  </TableHead>
                  <TableCell className="tabular">{ev.reminder.due_date}</TableCell>
                  <TableCell>{ev.kind === "escalation" ? <span className={chip({ state: "pending" })}><span aria-hidden="true">!</span>Escalation, {stageText(ev)}</span> : stageText(ev)}</TableCell>
                  <TableCell className="max-w-xs whitespace-normal">
                    {ev.recipients.length ? ev.recipients.map((x) => `${x.name} <${x.email}>`).join(", ") : <span className="text-muted-foreground">Nobody by email</span>}
                    {ev.fallback && <span className="block text-sm text-muted-foreground">{ev.fallback === "rule" ? "The rule's fallback stands in for the routed owner." : "The IT coordinator stands in for the routed owner."}</span>}
                    {ev.opted_out.length > 0 && <span className="block text-sm text-muted-foreground">Turned reminder emails off: {ev.opted_out.join(", ")}</span>}
                  </TableCell>
                  <TableCell className="max-w-xs whitespace-normal">{ev.destinations.length ? ev.destinations.map((d) => `${d.channel === "teams" ? "Teams" : "Webhook"}: ${d.name}`).join(", ") : "—"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
    </section>
  );
}

// ---- test send ------------------------------------------------------------------------------------------------------

function RuleTestSend({ rule }: { rule: NotificationRule }) {
  const me = useMe();
  const digest = rule.kind === "digest";
  const teamsDest = useApiQuery<Destination[]>("notification-settings/teams-destinations", { enabled: !digest });
  const hooks = useApiQuery<Destination[]>("notification-settings/webhook-destinations", { enabled: !digest });
  const [email, setEmail] = useState(true);
  const [teams, setTeams] = useState(rule.teams_destinations.find((d) => d.enabled)?.id ?? "");
  const [hook, setHook] = useState(rule.webhook_destinations.find((d) => d.enabled)?.id ?? "");
  const [result, setResult] = useState<RuleTestSendResult | null>(null);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  const invalidate = useInvalidate();
  async function send(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setResult(null);
    try {
      const body: Record<string, unknown> = { email };
      if (!digest) { body.teams_destination = teams || null; body.webhook_destination = hook || null; }
      const done = await api<RuleTestSendResult>(`${LIST}/${rule.id}/test-send`, { method: "POST", body });
      setErrors({});
      setResult(done);
      void invalidate("notification-log");
    } catch (err) {
      setErrors(err instanceof Problem && Object.keys(err.errors).length ? err.errors : { email: [problemText(err, "Couldn't send the test.")] });
    } finally {
      setBusy(false);
    }
  }
  // Until the organization's lists arrive (or if they can't), the rule's own channels stay choosable, so the chosen
  // one never reads "None"; a note under the select says what is missing, with Retry.
  const choices = (q: { data?: Destination[] }, own: { id: string; name: string; enabled: boolean }[]) =>
    (q.data ?? own).filter((d) => d.enabled);
  const listNote = (q: { isError: boolean; data?: Destination[]; error: unknown; refetch: () => unknown }, what: string, id: string) => (
    q.isError ? (
      <p className="-mt-2 mb-3 flex flex-wrap items-center gap-2 text-sm text-error-foreground" id={id}>
        <span><span aria-hidden="true">! </span>Couldn't load the {what}: {problemText(q.error, "try again")}. Only the rule's own are listed.</span>
        <Button type="button" size="sm" variant="outline" onClick={() => void q.refetch()}>Retry<span className="sr-only"> loading the {what}</span></Button>
      </p>
    ) : !q.data ? <p className="-mt-2 mb-3 text-sm text-muted-foreground" id={id} aria-live="polite">Loading the {what}…</p> : null
  );
  return (
    <section aria-labelledby="test-heading" className="max-w-2xl">
      <h2 id="test-heading" className="mb-1 font-display text-section font-semibold">Send a sample now</h2>
      <p className="mb-3 text-sm text-muted-foreground">
        {digest ? "Your own digest, as it would be built now, by email to you." : "Rendered for the rule's soonest reminder (else an example), by email to you and to a channel you choose."} Recorded in the notification log as a test.
      </p>
      <form onSubmit={send} noValidate className="rounded-lg border bg-card px-4 py-4">
        <ErrorSummary errors={errors} />
        <Check id="email" label={`Email to me (${me.user.email})`} checked={email} onChange={setEmail} />
        {!digest && <>
          <Field id="teams_destination" label="Teams channel" errors={errors} help="Only channels that are on.">
            <Select id="teams_destination" value={teams} onChange={(e) => setTeams(e.target.value)}>
              <option value="">None</option>
              {choices(teamsDest, rule.teams_destinations).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </Select>
          </Field>
          {listNote(teamsDest, "Teams channels", "teams-list-note")}
          <Field id="webhook_destination" label="Webhook" errors={errors}>
            <Select id="webhook_destination" value={hook} onChange={(e) => setHook(e.target.value)}>
              <option value="">None</option>
              {choices(hooks, rule.webhook_destinations).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </Select>
          </Field>
          {listNote(hooks, "webhooks", "webhooks-list-note")}
        </>}
        <Button type="submit" disabled={busy} aria-describedby="rule-test-result">{busy ? "Sending…" : "Send test"}</Button>
      </form>
      <div id="rule-test-result" className="mt-3" role="status">
        {result && <TestResultList results={result.results} />}
      </div>
    </section>
  );
}
