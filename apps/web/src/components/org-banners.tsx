/** The shell's organization states, from `auth/me`: the permanent impersonation banner, the deletion banner every member sees
 *  (with the final export for Admins), and the page a suspended organization's members get instead of every screen.
 *  Small and in the first bundle, because they show on every page. */
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { serverNow, type Me } from "@/api";
import { Button } from "@/components/ui/button";

const pad = (n: number) => String(n).padStart(2, "0");
/** "2026-12-08" in the organization's timezone ("—" for none). */
export function orgDate(iso: string | null | undefined, timeZone: string) {
  if (!iso) return "—";
  try { return new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(iso)); } catch { return iso.slice(0, 10); }
}
const dateIn = orgDate;
/** "14:32" in the browser's time zone. */
const clock = (iso: string) => { const d = new Date(iso); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };

/** Seconds left until `iso` by the server's clock (api.ts serverNow), updated every second; 0 once it has passed. */
function useSecondsLeft(iso: string) {
  const left = () => Math.max(0, Math.floor((new Date(iso).getTime() - serverNow()) / 1000));
  const [seconds, setSeconds] = useState(left);
  useEffect(() => {
    setSeconds(left());
    const t = setInterval(() => setSeconds(left()), 1000);
    return () => clearInterval(t);
  }, [iso]); // eslint-disable-line react-hooks/exhaustive-deps
  return seconds;
}

/** While platform staff act as this member: who, in which organization, why, the time left and Stop impersonating, on
 *  every page. Sticky, so it stays in view while the page scrolls, and the page's scroll padding is its height, so it
 *  never covers the focused control (WCAG 2.4.11). The countdown isn't announced each second.
 *
 *  At 0:00 `onCheck` reloads the session, and again every three seconds until the impersonation has ended (App returns to
 *  the console then): a re-check that finds it still live also corrects the countdown for the server's clock. */
export function ImpersonationBanner({ me, onStop, stopping, stopError, onCheck }: { me: Me; onStop: () => void; stopping: boolean; stopError: string; onCheck: () => void }) {
  const imp = me.impersonation!;
  const seconds = useSecondsLeft(imp.expires_at);
  const expired = seconds === 0;
  const latestCheck = useRef(onCheck);
  latestCheck.current = onCheck;
  useEffect(() => {
    if (!expired) return;
    latestCheck.current();
    const t = setInterval(() => latestCheck.current(), 3000);
    return () => clearInterval(t);
  }, [expired]);
  const box = useRef<HTMLElement>(null);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const root = document.documentElement;
    const pad = () => { root.style.scrollPaddingTop = `${el.offsetHeight}px`; };
    pad();
    const watch = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(pad);
    watch?.observe(el);
    return () => { watch?.disconnect(); root.style.scrollPaddingTop = ""; };
  }, []);
  return (
    <section ref={box} aria-label="Impersonation" data-testid="impersonation-banner"
      className="sticky top-0 z-40 flex flex-wrap items-center gap-x-4 gap-y-2 border-b-2 border-warning-border bg-warning-bg px-4 py-2.5 text-warning-foreground sm:px-6">
      <p className="min-w-0 flex-1">
        <span aria-hidden="true" className="font-bold">◐ </span>
        <strong>You are impersonating {me.user.name} ({me.user.email}) in {me.organization.name}</strong>
        <span className="block text-sm">
          As platform staff {imp.by.name}. Reason: {imp.reason}. Every change is audited as {me.user.name} with your name beside it.
        </span>
      </p>
      <p className="text-sm">
        <span className="font-semibold tabular" aria-hidden="true">{expired ? "Ending… checking" : <>Ends in {Math.floor(seconds / 60)}:{pad(seconds % 60)}</>}</span>
        <span className="sr-only">Ends at {clock(imp.expires_at)}</span>
        <span className="block" aria-hidden="true">at {clock(imp.expires_at)}</span>
      </p>
      <Button type="button" variant="outline" className="border-warning-border bg-card text-foreground" aria-busy={stopping || undefined} onClick={onStop}>
        {stopping ? "Stopping…" : "Stop impersonating"}
      </Button>
      {stopError && <p className="w-full text-sm font-semibold" role="alert"><span aria-hidden="true">! </span>{stopError}</p>}
    </section>
  );
}

/** Every member sees when the organization is removed; Admins also get the final export's state and its download, and a
 *  link to cancel. */
export function DeletionBanner({ me }: { me: Me }) {
  const deletion = me.organization_deletion!;
  const due = dateIn(deletion.due_at, me.organization.timezone);
  const exp = deletion.final_export;
  const admin = me.role === "admin";
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-l-4 border-error-border bg-error-bg px-4 py-2 sm:px-6" role="status" data-testid="deletion-banner">
      <p className="min-w-0 flex-1">
        <span aria-hidden="true" className="font-bold">! </span>
        <strong>{me.organization.name} is scheduled for deletion on <span className="tabular">{due}</span>.</strong>
        {" "}Every record, file and the audit log will be removed then{admin ? "; an Admin can cancel until then." : ". Ask an Admin if this is a mistake."}
        {admin && exp && (
          <span className="block text-sm" data-testid="final-export">
            Final export: {exp.status === "ready" && exp.download_url
              ? <>ready. <a href={exp.download_url} className="font-semibold">Download the final export</a>{exp.expires_at && <> (until <span className="tabular">{dateIn(exp.expires_at, me.organization.timezone)}</span>)</>}.</>
              : exp.status === "failed" ? "it failed. Softwarden retries it once; if it fails again, cancel the deletion and contact support."
              : exp.status === "expired" ? "its download has expired."
              : "being prepared. It can be downloaded here once it's ready."}
          </span>
        )}
      </p>
      {admin && <Link to="/admin/organization#delete-organization" className="font-semibold">Review or cancel the deletion</Link>}
    </div>
  );
}

/** A suspended organization's members get this instead of every page (the API refuses everything but `auth/me`, sign-out
 *  and switching organization). The sidebar stays, to switch or sign out. */
export function SuspendedPanel({ me, onSignOut, onCheck }: { me: Me; onSignOut: () => void; onCheck: () => Promise<unknown> }) {
  useEffect(() => { document.title = "Organization suspended · Softwarden"; }, []);
  const [checking, setChecking] = useState(false);
  const [status, setStatus] = useState("");
  async function check() {
    if (checking) return;
    setChecking(true); setStatus("");
    await onCheck();
    // Still here: still suspended (once lifted, the shell shows the page instead of this one).
    setChecking(false);
    setStatus(`Checked at ${clock(new Date().toISOString())}: still suspended.`);
  }
  return (
    <>
      <h1 tabIndex={-1} className="mb-4 font-display text-title font-bold text-text-strong outline-none">{me.organization.name} is suspended</h1>
      <div className="max-w-prose rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" data-testid="suspended">
        <p className="font-semibold"><span aria-hidden="true">⊘ </span>The service operator suspended this organization on <span className="tabular">{dateIn(me.organization_suspended!.suspended_at, me.organization.timezone)}</span>.</p>
        <p className="mt-1">Nothing has been removed: its records, files and audit log are kept. Nobody can use it until the operator restores access; contact them, or your organization's billing contact.</p>
        {me.organizations.length > 1 && <p className="mt-1">You can switch to another of your organizations from the sidebar.</p>}
        <div className="mt-3 flex flex-wrap gap-2">
          <Button type="button" variant="outline" aria-busy={checking || undefined} onClick={() => void check()}>{checking ? "Checking…" : "Check again"}</Button>
          <Button type="button" variant="outline" onClick={onSignOut}>Sign out</Button>
        </div>
        <p className="mt-2 text-sm" role="status">{status}</p>
      </div>
    </>
  );
}
