/** Administration › System status: the background worker's
 *  heartbeat, each schedule's last run and state, the job queue and the nightly backup with its warnings
 *  (system-backup.tsx), from `GET system` (Admin). Schedules and the
 *  heartbeat are installation-wide; job counts cover this organisation plus installation-wide jobs. Refreshed every
 *  30 seconds while the page is open and the browser tab is visible.
 *
 *  Two views (`scope` in the answer): the installation's operator (platform staff, or on a
 *  self-hosted install an Admin of its first organization) gets everything; any other Admin gets the `organization`
 *  view, without schedules, backups or installation-wide jobs, and with only its own stored-secrets warning. The
 *  platform console shows the installation view at /platform/system (`GET platform/system`, SystemStatusView). */
import type { UseQueryResult } from "@tanstack/react-query";
import { Activity, CalendarClock, KeyRound, ListChecks, TriangleAlert } from "lucide-react";
import { AdminOnly, AllClear, EmptyState, ErrorBanner, Loading, PageHead, TableRegion } from "@/components/app";
import { chip, DetailList, DetailRow, Panel } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { BackupPanel, code } from "@/features/admin/system-backup";
import { useTypedQuery, type Schemas } from "@/lib/openapi";
import { problemText } from "@/lib/query";
import { useMe } from "@/lib/session";

export type SystemStatus = Schemas["SystemStatus"];
export type WorkerState = SystemStatus["worker"]["state"];
export type ScheduleState = NonNullable<SystemStatus["schedules"]>[number]["state"];
type JobCounts = Schemas["JobCounts"];
type Warning = Schemas["SystemWarning"];

export const REFRESH_MS = 30_000;

type Tone = "confirmed" | "pending" | "overdue" | "unreviewed" | "neutral";
function StatusChip({ tone, glyph, label }: { tone: Tone; glyph: string; label: string }) {
  return <span className={chip({ state: tone })}><span aria-hidden="true">{glyph}</span>{label}</span>;
}

const OVERALL: Record<SystemStatus["status"], { tone: Tone; glyph: string; label: string }> = {
  ok: { tone: "confirmed", glyph: "✓", label: "OK" },
  degraded: { tone: "pending", glyph: "◐", label: "Degraded" },
  unhealthy: { tone: "overdue", glyph: "!", label: "Unhealthy" },
};
const WORKER: Record<WorkerState, { tone: Tone; glyph: string; label: string }> = {
  ok: { tone: "confirmed", glyph: "✓", label: "Running" },
  stale: { tone: "overdue", glyph: "!", label: "Stale" },
  never: { tone: "unreviewed", glyph: "○", label: "Never seen" },
};
/** Worst first, as core/health.py orders them. Retired rows are no longer part of this version and never count. */
export const SCHEDULE: Record<ScheduleState, { tone: Tone; glyph: string; label: string }> = {
  stuck: { tone: "overdue", glyph: "!", label: "Stuck" },
  overdue: { tone: "overdue", glyph: "!", label: "Overdue" },
  failing: { tone: "pending", glyph: "✕", label: "Failing" },
  running: { tone: "neutral", glyph: "▸", label: "Running" },
  never_run: { tone: "unreviewed", glyph: "○", label: "Never run" },
  disabled: { tone: "neutral", glyph: "–", label: "Disabled" },
  ok: { tone: "confirmed", glyph: "✓", label: "OK" },
  retired: { tone: "neutral", glyph: "—", label: "Retired" },
};
const JOB_STATUS = {
  failed: { tone: "overdue", glyph: "✕", label: "Failed" },
  abandoned: { tone: "pending", glyph: "!", label: "Abandoned" },
} as const;

const pad = (n: string | number) => String(n).padStart(2, "0");

/** A five-field cron (UTC) in words where it reads simply; null otherwise (the cron itself is always shown). */
export function cronInWords(cron: string): string | null {
  const f = cron.trim().split(/\s+/);
  if (f.length !== 5) return null;
  const [m, h, dom, mon, dow] = f;
  if (dom !== "*" || mon !== "*" || dow !== "*") return null;
  const num = (v: string) => /^\d+$/.test(v);
  if (m === "*" && h === "*") return "Every minute";
  const step = /^\*\/(\d+)$/.exec(m);
  if (step && h === "*") return `Every ${step[1]} minutes`;
  if (num(m) && h === "*") return `Every hour at minute ${Number(m)}`;
  if (num(m) && num(h)) return `Daily at ${pad(h)}:${pad(m)} UTC`;
  return null;
}

/** "2026-11-04 10:12" in the organisation's timezone. */
export function orgTime(iso: string, timeZone: string) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date(iso)).map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

/** "42 seconds", "3 minutes", "2 hours", "5 days". */
export function span(seconds: number) {
  const s = Math.max(0, Math.round(Math.abs(seconds)));
  const [n, unit] = s < 90 ? [s, "second"] : s < 90 * 60 ? [Math.round(s / 60), "minute"] : s < 36 * 3600 ? [Math.round(s / 3600), "hour"] : [Math.round(s / 86400), "day"];
  return `${n} ${unit}${n === 1 ? "" : "s"}`;
}

/** Relative to when the API checked, so the words match the state it computed. */
function relative(iso: string, checkedAt: string) {
  const diff = (new Date(iso).getTime() - new Date(checkedAt).getTime()) / 1000;
  if (Math.abs(diff) < 1) return "now";
  return diff < 0 ? `${span(diff)} ago` : `in ${span(diff)}`;
}

function When({ iso, checkedAt, tz, none = "Never" }: { iso: string | null; checkedAt: string; tz: string; none?: string }) {
  if (!iso) return <span className="text-muted-foreground">{none}</span>;
  return (
    <span className="whitespace-nowrap">
      <time dateTime={iso} className="tabular">{orgTime(iso, tz)}</time>
      <span className="block text-xs text-muted-foreground">{relative(iso, checkedAt)}</span>
    </span>
  );
}

/** Backup warnings belong to the backup panel; the others (stored secrets) are listed on their own. */
const isBackupWarning = (w: Warning) => w.code.startsWith("backup_");

/** Why the overall status is what it is, from the parts the API judged it by (core/health.overall). The organization
 *  view has no schedules: the status still counts the installation's, so a schedule problem is said without
 *  naming one. */
function overallReasons(s: SystemStatus) {
  const reasons: string[] = [];
  if (s.worker.state === "never") reasons.push("No worker has ticked against this database yet.");
  if (s.worker.state === "stale") reasons.push(`The worker's last tick was ${span(s.worker.age_seconds ?? 0)} ago (stale after ${span(s.worker.stale_after_seconds)}).`);
  if (!s.schedules) {
    if (!reasons.length && s.status !== "ok") reasons.push("One of the installation's schedules is behind or failing. Its operator sees which and fixes it; your data isn't affected by waiting.");
    if (!reasons.length) reasons.push("The worker is ticking and the installation's schedules are on time.");
  } else {
    const named = (state: ScheduleState) => s.schedules!.filter((c) => c.state === state).map((c) => c.label);
    const stuck = named("stuck"), overdue = named("overdue"), failing = named("failing");
    if (stuck.length) reasons.push(`Stuck: ${stuck.join(", ")}.`);
    if (overdue.length) reasons.push(`Overdue: ${overdue.join(", ")}.`);
    if (failing.length) reasons.push(`Last run failed: ${failing.join(", ")}.`);
    if (!reasons.length) reasons.push("The worker is ticking and no schedule is stuck, overdue or failing.");
    const never = named("never_run").length, disabled = named("disabled").length;
    if (never) reasons.push(`${never} ${never === 1 ? "schedule hasn't" : "schedules haven't"} run yet.`);
    if (disabled) reasons.push(`${disabled} ${disabled === 1 ? "schedule is" : "schedules are"} turned off by an operator.`);
  }
  const warned = s.warnings.length;
  if (warned) reasons.push(`${warned} ${warned === 1 ? "warning" : "warnings"} to fix (below). Warnings don't change the overall status.`);
  return reasons;
}

export function SystemStatusPage() {
  return (
    <AdminOnly title="System status" what="the background worker, this organization's jobs and, for the installation's operator, its schedules and backups">
      <OrganizationSystemStatus />
    </AdminOnly>
  );
}

function OrganizationSystemStatus() {
  const me = useMe();
  // Refetched every 30 s; TanStack Query pauses the interval while the browser tab is hidden.
  const q = useTypedQuery("system", {}, { refetchInterval: REFRESH_MS, refetchIntervalInBackground: false });
  const cloud = me.deployment === "cloud";
  // Who sees the installation is the API's call (`scope`); the words name who that is.
  const desc = q.data?.scope === "organization"
    ? `The background worker and ${me.organization.name}'s own jobs. The installation's schedules and backups are its operator's: ${cloud ? "the service operator watches them" : "platform staff see them here, and so do the Admins of the installation's first organization unless it is on the Cloud (billed) edition"}.`
    : "The background worker, its schedules, the job queue and backups. Schedules, the worker and backups are shared by the whole installation; jobs are this organization's and installation-wide ones.";
  return <SystemStatusView q={q} tz={me.organization.timezone} desc={desc} viewer="organization" cloud={cloud} />;
}

type StatusQuery = UseQueryResult<SystemStatus>;

/** The page itself, for an organization's Admin (`GET system`) and for platform staff (`GET platform/system`, the
 *  console's System page; `viewer` "platform": the browser's timezone, and the backup key acknowledgement through
 *  the platform route). */
export function SystemStatusView({ q, tz, desc, viewer, cloud = false }: { q: StatusQuery; tz: string; desc: string; viewer: "organization" | "platform"; cloud?: boolean }) {
  const s = q.data;
  const head = (
    <PageHead title="System status" desc={desc}>
      <Button variant="outline" onClick={() => void q.refetch()} disabled={q.isFetching}>{q.isFetching && s ? "Checking…" : "Check now"}</Button>
    </PageHead>
  );
  if (q.isError && !s) return <>{head}<ErrorBanner message={`${problemText(q.error, "Couldn't load the system status.")} Nothing was changed.`} onRetry={() => void q.refetch()} /></>;
  if (!s) return <>{head}<Loading what="system status" rows={4} /></>;
  const overall = OVERALL[s.status];
  const others = s.warnings.filter((w) => !isBackupWarning(w));
  const ownView = s.scope === "organization";
  return (
    <>
      {head}
      {q.isError && <ErrorBanner message={`Couldn't refresh: ${problemText(q.error, "the server didn't answer.")} Showing the status checked at ${orgTime(s.checked_at, tz)}.`} onRetry={() => void q.refetch()} />}
      <section aria-labelledby="h-overall" className="mb-5 rounded-lg border bg-card px-4 py-3" data-testid="overall-status">
        <div className="flex flex-wrap items-center gap-3">
          <h2 id="h-overall" className="font-display text-section font-semibold">Overall</h2>
          <StatusChip {...overall} />
          <span className="text-sm text-muted-foreground">Checked <time dateTime={s.checked_at} className="tabular">{orgTime(s.checked_at, tz)}</time>; refreshes every 30 seconds while this page is open.</span>
        </div>
        <ul className="mt-2 ml-5 list-disc">{overallReasons(s).map((r) => <li key={r}>{r}</li>)}</ul>
        {ownView && (
          <p className="mt-2 max-w-prose text-sm text-muted-foreground" data-testid="organization-view">
            <span aria-hidden="true">ⓘ </span>This is {cloud ? "a shared service" : "an installation shared with other organizations"}, so this page shows your organization's part only: no schedules, backups or other organizations' jobs.
            {" "}{cloud ? "The service operator runs the installation." : "The installation's operator sees them on this page: platform staff, and the Admins of its first organization unless that one is on the Cloud (billed) edition."}
          </p>
        )}
      </section>

      <div className="grid gap-5">
        {others.length > 0 && <WarningsPanel warnings={others} ownView={ownView} />}
        <Panel id="h-worker" icon={<Activity className="size-4" aria-hidden="true" />} title="Worker">
          <DetailList>
            <DetailRow label="State"><StatusChip {...WORKER[s.worker.state]} /></DetailRow>
            <DetailRow label="Last tick">
              {s.worker.last_tick
                ? <><time dateTime={s.worker.last_tick} className="tabular">{orgTime(s.worker.last_tick, tz)}</time> <span className="text-muted-foreground">({span(s.worker.age_seconds ?? 0)} ago)</span></>
                : <span className="text-muted-foreground">Never. Start the worker (<code className="font-mono text-[13px]">manage.py run_worker</code>, the <code className="font-mono text-[13px]">worker</code> service in Docker Compose).</span>}
            </DetailRow>
            <DetailRow label="Counts as stale after">{span(s.worker.stale_after_seconds)} without a tick</DetailRow>
          </DetailList>
        </Panel>

        {/* The `organization` view (an Admin of one organization among others) has no backup
            block and no schedules. */}
        {s.backup && <BackupPanel backup={s.backup} warnings={s.warnings.filter(isBackupWarning)} tz={tz} fmt={{ span, orgTime, cronInWords }}
          acknowledgePath={viewer === "platform" ? "platform/system/backup/acknowledge-key" : "system/backup/acknowledge-key"}
          statusPath={viewer === "platform" ? "platform/system" : "system"} />}

        {s.schedules && <Panel id="h-schedules" icon={<CalendarClock className="size-4" aria-hidden="true" />} title="Schedules" count={s.schedules.length} note={viewer === "platform" ? "Times in your browser's timezone; cron in UTC" : "Times in the organization's timezone; cron in UTC"}>
          {s.schedules.length === 0
            ? <div className="p-4"><EmptyState title="No schedules">This version registers no recurring work, so there is nothing to show.</EmptyState></div>
            : <ScheduleTable schedules={s.schedules} checkedAt={s.checked_at} tz={tz} />}
        </Panel>}

        <Panel id="h-jobs" icon={<ListChecks className="size-4" aria-hidden="true" />} title="Jobs">
          <JobCountsTable jobs={s.jobs} />
        </Panel>

        <Panel id="h-problems" icon={<TriangleAlert className="size-4" aria-hidden="true" />} title="Recent problems" count={s.jobs.recent_problems.length} note="Latest failed or abandoned jobs, newest first">
          {s.jobs.recent_problems.length === 0
            ? <div className="p-4"><AllClear>No failed or abandoned jobs.</AllClear></div>
            : <ProblemsTable problems={s.jobs.recent_problems} tz={tz} />}
        </Panel>
      </div>
      {/* The image serves the third-party license notices beside the SPA (generated at image build, so a
          development server may answer 404). */}
      <p className="mt-6 text-sm text-muted-foreground" data-testid="third-party-notices">
        Softwarden ships with open-source components under their own licenses: <a className="underline" href="/THIRD_PARTY_NOTICES.txt">Third-party notices</a>.
      </p>
    </>
  );
}


/** Warnings that aren't the backup's (stored secrets a missing credential key can't read), each with its fix:
 *  the operator's in `manage.py doctor`'s words, an organization's Admin's in theirs (they can't set the key). */
function WarningsPanel({ warnings, ownView }: { warnings: Warning[]; ownView: boolean }) {
  return (
    <Panel id="h-warnings" icon={<KeyRound className="size-4" aria-hidden="true" />} title="To fix" count={warnings.length} note="Warnings never change the overall status">
      <ul className="m-0 grid list-none gap-3 px-4 py-3" aria-label="Warnings" data-testid="system-warnings">
        {warnings.map((w) => (
          <li key={w.code} className="grid gap-1" data-code={w.code}>
            <span className="flex flex-wrap items-center gap-2">
              {w.code === "stored_secrets_unreadable"
                ? <StatusChip tone="overdue" glyph="⊘" label="Secrets unreadable" />
                : <StatusChip tone="pending" glyph="!" label="Warning" />}
              <span className="font-medium">{w.message}</span>
            </span>
            {w.code === "stored_secrets_unreadable" && (
              <p className="max-w-prose text-sm text-muted-foreground">
                {ownView
                  ? <>Some of this organization's connections, ticket connections, email or single sign-on settings hold a secret that can't be read, usually after the installation was restored with a new credential key. Enter those secrets again where they are set, or ask the installation's operator to bring back the old key.</>
                  : <>Usually a restore into a stack with a new credential key: the key is never in a backup. Bring the old installation's {code("credential_keys")} from its off-host copy and set {code("SOFTWARDEN_CREDENTIAL_KEYS")} to it (newest first), then {code("docker compose up -d")}; or enter those secrets again. {code("manage.py doctor")} counts them.</>}
              </p>
            )}
          </li>
        ))}
      </ul>
    </Panel>
  );
}

function ScheduleTable({ schedules, checkedAt, tz }: { schedules: NonNullable<SystemStatus["schedules"]>; checkedAt: string; tz: string }) {
  return (
    <TableRegion label="Schedules table" className="rounded-none border-0">
      <Table>
        <TableCaption className="sr-only">Schedules, in the order the worker runs them, then retired ones</TableCaption>
        <TableHeader><TableRow>
          <TableHead scope="col">Schedule</TableHead><TableHead scope="col">State</TableHead><TableHead scope="col">Runs</TableHead>
          <TableHead scope="col">Enabled</TableHead><TableHead scope="col">Last started</TableHead><TableHead scope="col">Last finished</TableHead>
          <TableHead scope="col">Last succeeded</TableHead><TableHead scope="col">Last error</TableHead><TableHead scope="col">Next due</TableHead>
        </TableRow></TableHeader>
        <TableBody>
          {schedules.map((c) => {
            const words = cronInWords(c.cron);
            return (
              <TableRow key={c.name}>
                <TableHead scope="row" className="min-w-48 whitespace-normal font-semibold text-foreground">
                  {c.label}
                  <span className="block font-mono text-xs font-normal text-muted-foreground">{c.name}</span>
                </TableHead>
                <TableCell className="min-w-40 whitespace-normal">
                  <StatusChip {...SCHEDULE[c.state]} />
                  {/* A failing schedule's detail is its error type, already in Last error. */}
                  {c.detail && c.detail !== c.last_error && <span className="mt-1 block max-w-64 text-xs text-muted-foreground">{c.detail}</span>}
                </TableCell>
                <TableCell>
                  {words ?? <code className="font-mono text-[13px]">{c.cron}</code>}
                  {words && <span className="block font-mono text-xs text-muted-foreground">{c.cron}</span>}
                </TableCell>
                <TableCell>{c.enabled ? "Yes" : "No"}</TableCell>
                <TableCell><When iso={c.last_started} checkedAt={checkedAt} tz={tz} /></TableCell>
                <TableCell><When iso={c.last_finished} checkedAt={checkedAt} tz={tz} /></TableCell>
                <TableCell><When iso={c.last_succeeded} checkedAt={checkedAt} tz={tz} /></TableCell>
                <TableCell>{c.last_error ? <code className="font-mono text-[13px]">{c.last_error}</code> : <span className="text-muted-foreground">None</span>}</TableCell>
                <TableCell><When iso={c.state === "retired" || c.state === "disabled" ? null : c.next_due} checkedAt={checkedAt} tz={tz} none="—" /></TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </TableRegion>
  );
}

const COUNT_ROWS: [keyof JobCounts, string][] = [["queued", "Queued"], ["running", "Running"], ["succeeded", "Succeeded"], ["failed", "Failed"], ["abandoned", "Abandoned"]];

/** Job counts: this organization's and installation-wide, or only the columns the view has (the organization view has
 *  no installation-wide jobs; the console's has no organization). */
function JobCountsTable({ jobs }: { jobs: SystemStatus["jobs"] }) {
  const mine = !!jobs.organization, system = !!jobs.system;
  const whose = mine && system ? "for this organization and installation-wide" : mine ? "for this organization" : "installation-wide";
  return (
    <TableRegion label="Job counts table" className="rounded-none border-0">
      <Table>
        <TableCaption className="sr-only">Job counts by status, {whose}. Finished jobs are kept 30 days.</TableCaption>
        <TableHeader><TableRow>
          <TableHead scope="col">Status</TableHead>
          {mine && <TableHead scope="col" className="text-right">This organization</TableHead>}
          {system && <TableHead scope="col" className="text-right">Installation-wide</TableHead>}
        </TableRow></TableHeader>
        <TableBody>
          {COUNT_ROWS.map(([key, label]) => (
            <TableRow key={key}>
              <TableHead scope="row" className="font-normal text-foreground">{label}</TableHead>
              {mine && <TableCell className="tabular text-right">{jobs.organization![key]}</TableCell>}
              {system && <TableCell className="tabular text-right">{jobs.system![key]}</TableCell>}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableRegion>
  );
}

function ProblemsTable({ problems, tz }: { problems: SystemStatus["jobs"]["recent_problems"]; tz: string }) {
  return (
    <TableRegion label="Recent problems table" className="rounded-none border-0">
      <Table>
        <TableCaption className="sr-only">Failed or abandoned jobs, newest first</TableCaption>
        <TableHeader><TableRow>
          <TableHead scope="col" aria-sort="descending">Finished</TableHead><TableHead scope="col">Job</TableHead><TableHead scope="col">Status</TableHead>
          <TableHead scope="col">Scope</TableHead><TableHead scope="col" className="text-right">Attempts</TableHead><TableHead scope="col">Error</TableHead>
        </TableRow></TableHeader>
        <TableBody>
          {problems.map((p) => (
            <TableRow key={p.id}>
              <TableHead scope="row" className="tabular font-normal text-foreground">{p.finished_at ? <time dateTime={p.finished_at}>{orgTime(p.finished_at, tz)}</time> : "Not recorded"}</TableHead>
              <TableCell><code className="font-mono text-[13px]">{p.kind}</code></TableCell>
              <TableCell><StatusChip {...JOB_STATUS[p.status]} /></TableCell>
              <TableCell>{p.scope === "system" ? "Installation-wide" : "This organization"}</TableCell>
              <TableCell className="tabular text-right">{p.attempts}</TableCell>
              <TableCell className="max-w-md whitespace-normal [overflow-wrap:anywhere]">{p.last_error || <span className="text-muted-foreground">None recorded</span>}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableRegion>
  );
}
