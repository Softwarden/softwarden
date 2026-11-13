/** App-level building blocks on top of shadcn/ui, following the UI rules in docs/DEVELOPMENT.md. */
import { cloneElement, isValidElement, lazy, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { keepPreviousData } from "@tanstack/react-query";
import { Problem } from "@/api";
import { problemText, useApiQuery } from "@/lib/query";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { useMe } from "@/lib/session";
import { cn } from "@/lib/utils";

/** The original loader, now a thin layer over TanStack Query (src/lib/query.ts) so its pages share the cache and are
 *  refreshed by `useApiMutation`/`useInvalidate`. New code should call `useApiQuery` directly.
 *
 *  `strict`: while a new path loads, or if it fails, `data` is null rather than the previous path's (once
 *  Needs attention showed one scope's rows under another). The query key is the exact path, so a late response for an
 *  earlier path can only ever land in that path's cache entry. `latest` is whatever arrived last, for values that don't
 *  depend on the path, such as the scope counts.
 *
 *  Behaviour kept from before: nothing is cached once the screen unmounts (gcTime 0), so returning to a page loads it
 *  afresh; a reload clears the error while it runs. */
export function useLoad<T>(path: string, { strict = false }: { strict?: boolean } = {}) {
  const q = useApiQuery<T>(path, { placeholderData: keepPreviousData, gcTime: 0 });
  const [last, setLast] = useState<T | null>(null);
  useEffect(() => { if (q.data !== undefined) setLast(q.data); }, [q.data]);
  const fresh = q.data !== undefined && !q.isPlaceholderData ? q.data : null;
  const failed = q.isError && !q.isFetching;
  const { refetch } = q;
  const reload = useCallback(() => { void refetch(); }, [refetch]);
  return {
    data: strict ? fresh : (q.data ?? last),
    error: failed ? problemText(q.error, "Couldn't load this page.") : "",
    status: failed && q.error instanceof Problem ? q.error.status : 0,
    reload,
    latest: q.data ?? last,
  };
}

/** Radix returns focus to a DialogTrigger only; these dialogs open from state, so remember the opener and focus it on close.
 *  Spread the result on DialogContent. The opener must still be mounted when the dialog closes.
 *
 *  `fallback` names where focus goes when the opener is gone by then, or was never a real control (a dialog opened from
 *  another dialog's button, which unmounted with it): the empty state's button replaced by the table's header button,
 *  or the row button that opened the first dialog. Without one, or when it is gone too, the page's heading. */
export function useReturnFocus(open: boolean, fallback?: () => HTMLElement | null | undefined) {
  const opener = useRef<HTMLElement | null>(null);
  // A layout effect runs before Radix moves focus into the dialog.
  useLayoutEffect(() => { if (open) opener.current = document.activeElement as HTMLElement | null; }, [open]);
  // An opener that is gone (the row a delete removed) leaves focus on the fallback or the page's heading, never the body.
  return {
    onCloseAutoFocus: (e: Event) => {
      e.preventDefault();
      const was = opener.current;
      if (was?.isConnected && was !== document.body) return was.focus();
      const other = fallback?.();
      if (other?.isConnected) other.focus();
      else document.querySelector<HTMLElement>("main h1")?.focus();
    },
  };
}

export function Loading({ what, rows = 0 }: { what: string; rows?: number }) {
  return (
    <div aria-live="polite">
      <p className="text-muted-foreground">Loading {what}…</p>
      {rows > 0 && (
        <div className="mt-3 overflow-hidden rounded-lg border bg-card" aria-hidden="true">
          {Array.from({ length: rows }, (_, i) => (
            <div key={i} className="flex items-center gap-4 border-b border-border-subtle px-4 py-4 last:border-0">
              <span className="h-3.5 w-1/4 rounded-sm bg-muted" /><span className="h-3.5 w-16 rounded-sm bg-muted" /><span className="h-3.5 w-1/3 rounded-sm bg-muted" />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Empty state: what the area holds, the minimum data needed, one next step. */
export function EmptyState({ title, children, action }: { title: string; children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="rounded-lg border bg-card px-4 py-5">
      <p className="font-semibold">{title}</p>
      <p className="mt-1 max-w-prose text-muted-foreground">{children}</p>
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}

export function ErrorBanner({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="mb-4 flex flex-wrap items-center gap-3 rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert">
      <strong>{message}</strong>
      {onRetry && <Button size="sm" variant="outline" onClick={onRetry}>Retry</Button>}
    </div>
  );
}

export function AllClear({ children }: { children: React.ReactNode }) {
  return <p className="rounded-md border bg-card px-3 py-2 text-success-foreground">✓ {children}</p>;
}

export { ChipGroup, GapChip as Gap, GapChips, ReviewStateChip as StateChip } from "@/components/sw";
import { chip as chipVariants } from "@/components/sw";

export function Chip({ children, className }: { children: React.ReactNode; className?: string }) {
  return <span className={cn(chipVariants({ state: "neutral" }), className)}>{children}</span>;
}

export function PageHead({ title, desc, children, docTitle }: { title: React.ReactNode; desc?: React.ReactNode; children?: React.ReactNode; docTitle?: string }) {
  // Each route gets its own document title so tabs, history and screen readers can tell pages apart.
  const text = docTitle ?? (typeof title === "string" ? title : undefined);
  useEffect(() => { if (text) document.title = `${text} · Softwarden`; }, [text]);
  return (
    <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 tabIndex={-1} className="font-display text-title font-bold text-text-strong outline-none">{title}</h1>
        {desc && <div className="mt-1 max-w-prose text-muted-foreground">{desc}</div>}
      </div>
      {children && <div className="flex flex-wrap gap-2">{children}</div>}
    </div>
  );
}

/** Whether a load failed because the record isn't there (an API 404). */
export const isNotFound = (error: unknown) => error instanceof Problem && error.status === 404;

/** A record or page that isn't there (an API 404): a "not found" heading, why, and the way back to its list. No
 *  Retry: asking again can't find it. `what` is the noun ("renewal"); `back` is the list it belongs to. */
export function NotFound({ what, back, trail }: { what: string; back: { to: string; label: string }; trail?: { to: string; label: string }[] }) {
  const noun = what.charAt(0).toUpperCase() + what.slice(1);
  return (
    <>
      {trail && <Breadcrumb trail={trail} current={`${noun} not found`} />}
      <PageHead title={`${noun} not found`} desc={`This ${what} doesn't exist, or you don't have access to it. It may have been deleted, or the link may be incomplete.`} />
      <Button asChild variant="outline"><Link to={back.to}>Back to {back.label}</Link></Button>
    </>
  );
}

export function Breadcrumb({ trail, current }: { trail: { to: string; label: string }[]; current: string }) {
  return (
    <nav className="mb-1.5 text-sm text-muted-foreground" aria-label="Breadcrumb">
      <ol className="flex flex-wrap items-center gap-1.5">
        {trail.map((t) => <li key={t.to} className="flex items-center gap-1.5"><Link to={t.to}>{t.label}</Link><span aria-hidden="true">/</span></li>)}
        <li aria-current="page">{current}</li>
      </ol>
    </nav>
  );
}

/** The labelled, focusable box a wide table scrolls in. ui/Table has no scroll box of its own, so this
 *  is the one that scrolls. `className` adjusts the frame (a table inside a bordered panel drops its own border). */
export function TableRegion({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("relative overflow-x-auto rounded-lg border bg-card", className)} role="region" aria-label={label} tabIndex={0}>
      {children}
    </div>
  );
}

export function Field({ id, label, errors, help, children }: {
  id: string; label: string; errors?: Record<string, string[]>; help?: React.ReactNode; children: React.ReactNode;
}) {
  const err = errors?.[id];
  // Wire the error and help text to the control so screen readers read them with the label.
  const describedBy = [err && `${id}-error`, help && `${id}-help`].filter(Boolean).join(" ") || undefined;
  const control = isValidElement<Record<string, unknown>>(children)
    ? cloneElement(children, { "aria-invalid": err ? true : children.props["aria-invalid"], "aria-describedby": describedBy ?? children.props["aria-describedby"] })
    : children;
  return (
    <div className="mb-3 flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {control}
      {help && <span className="text-sm text-muted-foreground" id={`${id}-help`}>{help}</span>}
      {err && <span className="text-sm text-error-foreground" id={`${id}-error`}>{err.join(" ")}</span>}
    </div>
  );
}

/** `focusOnShow` moves focus to the summary when errors appear (full-page forms); `"field"` moves it to the first
 *  invalid field on the page instead (the record, integration and vendor forms: the summary is still read out, as an
 *  alert). Links focus the field they name. */
export function ErrorSummary({ errors, focusOnShow = false }: { errors: Record<string, string[]>; focusOnShow?: boolean | "field" }) {
  const keys = Object.keys(errors);
  const box = useRef<HTMLDivElement>(null);
  const signature = keys.join("|") + Object.values(errors).join("|");
  useEffect(() => {
    if (!focusOnShow || !signature) return;
    if (focusOnShow === "field") {
      const first = keys.map((k) => document.getElementById(k)).filter((el): el is HTMLElement => !!el)
        .sort((x, y) => (x.compareDocumentPosition(y) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1))[0];
      if (first) { first.focus(); first.scrollIntoView({ block: "center" }); return; }
    }
    box.current?.focus();
  }, [focusOnShow, signature]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!keys.length) return null;
  const focusField = (k: string) => (e: React.MouseEvent) => {
    const el = document.getElementById(k);
    if (!el) return;
    e.preventDefault();
    el.focus();
    el.scrollIntoView({ block: "center" });
  };
  return (
    <div ref={box} tabIndex={-1} className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3 outline-none focus-visible:ring-2 focus-visible:ring-ring" role="alert">
      <strong>Fix {keys.length === 1 ? "this field" : "these fields"}:</strong>
      <ul className="ml-5 list-disc">{keys.map((k) => <li key={k}><a href={`#${k}`} className="inline-block min-h-6 leading-6" onClick={focusField(k)}>{errors[k].join(" ")}</a></li>)}</ul>
    </div>
  );
}

export function Textarea(props: React.ComponentProps<"textarea">) {
  return (
    <textarea rows={3} {...props}
      className={cn("w-full min-w-0 rounded-md border border-input bg-transparent px-3 py-2 text-base shadow-xs outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 aria-invalid:border-destructive disabled:opacity-50 md:text-sm", props.className)} />
  );
}

export function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={cn("native-select", props.className)} />;
}

/** Run a write; return field errors or the problem detail instead of throwing. */
export async function attempt(fn: () => Promise<unknown>): Promise<{ errors: Record<string, string[]>; detail: string; status?: number } | null> {
  try {
    await fn();
    return null;
  } catch (e) {
    // `status`: a 412 (the record changed since it was read) shows the form's "Load the latest version".
    if (e instanceof Problem) return { errors: e.errors, detail: e.detail, status: e.status };
    return { errors: {}, detail: "Something went wrong. Try again." };
  }
}

/** Tab strip (WAI-ARIA tabs, automatic activation): arrows, Home and End move between tabs. The panel should be
 *  `<div role="tabpanel" id="tabpanel" aria-labelledby={`tab-${tab}`}>`. A second strip on the same page (tabs inside a
 *  tab) passes `idPrefix`: its ids are `${idPrefix}tab-…` and its panel `${idPrefix}tabpanel`. */
export function TabStrip<T extends string>({ tabs, tab, onSelect, label, idPrefix = "" }: { tabs: { id: T; label: string }[]; tab: T; onSelect: (t: T) => void; label: string; idPrefix?: string }) {
  function onKey(e: React.KeyboardEvent) {
    const at = tabs.findIndex((t) => t.id === tab);
    const to = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : e.key === "ArrowRight" ? (at + 1) % tabs.length : e.key === "ArrowLeft" ? (at - 1 + tabs.length) % tabs.length : -1;
    if (to < 0) return;
    e.preventDefault();
    onSelect(tabs[to].id);
    document.getElementById(`${idPrefix}tab-${tabs[to].id}`)?.focus();
  }
  return (
    // Tabs wrap onto a second row on a narrow screen rather than scroll out of sight at the edge.
    <div className="mb-4 flex flex-wrap gap-x-1 border-b" role="tablist" aria-label={label} onKeyDown={onKey}>
      {tabs.map((t) => (
        <button key={t.id} role="tab" type="button" id={`${idPrefix}tab-${t.id}`} aria-selected={tab === t.id} aria-controls={`${idPrefix}tabpanel`} tabIndex={tab === t.id ? 0 : -1}
          onClick={() => onSelect(t.id)}
          className="-mb-px whitespace-nowrap border-b-[3px] border-transparent px-3 py-2 text-sm text-muted-foreground aria-selected:border-accent-line aria-selected:font-semibold aria-selected:text-link">
          {t.label}
        </button>
      ))}
    </div>
  );
}

/** An Admin-only page opened by another role (a shared link, a bookmark): say who may open it instead of "Page not
 *  found", and hide nothing else (permission denied). */
export function AdminOnly({ title, what, children }: { title: string; what: string; children: React.ReactNode }) {
  const me = useMe();
  if (me.role === "admin") return <>{children}</>;
  return (
    <>
      <PageHead title={title} />
      <div className="rounded-lg border bg-card px-4 py-5" data-testid="admin-only">
        <p className="font-semibold"><span aria-hidden="true">! </span>Only Admins can open {title}.</p>
        <p className="mt-1 max-w-prose text-muted-foreground">
          It shows {what}. You're signed in as {me.role === "editor" ? "an Editor" : "a Viewer"} in {me.organization.name}; ask one of its Admins if you need it.
        </p>
        <div className="mt-3"><Button asChild variant="outline"><Link to="/">Go to Needs attention</Link></Button></div>
      </div>
    </>
  );
}

/** Shown in place of a lazily loaded part of a page (a tab, a dialog, a settings section) whose code can't be loaded
 *  (offline, or the app was updated since the page loaded); the rest of the page keeps working. */
export function PartUnavailable() {
  return (
    <div className="rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert">
      <p className="font-semibold">Couldn't load this part of the page. Reload the page to try again.</p>
      <p className="mt-1">Nothing you saved is affected.</p>
    </div>
  );
}

/** A component loaded on first use, so its code stays out of the first bundle (attachments, the
 *  exception dialog, settings sections). A failed load shows PartUnavailable. Render it inside <LazyPart>. */
export function lazyPart<P extends object>(load: () => Promise<React.ComponentType<P>>) {
  return lazy(() => load().then((C) => ({ default: C })).catch(() => ({ default: PartUnavailable as unknown as React.ComponentType<P> })));
}

export function LazyPart({ what, children }: { what: string; children: React.ReactNode }) {
  return <Suspense fallback={<p className="text-muted-foreground" aria-live="polite">Loading {what}…</p>}>{children}</Suspense>;
}
