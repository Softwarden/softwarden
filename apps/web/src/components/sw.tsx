/** Softwarden-specific components from the design handoff. */
import { cva } from "class-variance-authority";
import { Archive, CircleCheck, FlaskConical, Lightbulb, TriangleAlert, type LucideIcon } from "lucide-react";
import { KIND_ICON, KIND_LABEL, type CatalogKind } from "@/lib/kinds";
import { cap, type LinkState, type ReviewState, type Steward, type TagColor } from "@/api";
import { useMe } from "@/lib/session";
import { cn } from "@/lib/utils";

/** The logo ("Record S"): an S drawn as one line through three ledger rows, on a rounded tile in the primary color.
 *  The same mark as the website's and public/favicon.svg. */
export function BrandMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={cn("h-7 w-7", className)} aria-hidden="true">
      <rect x="2" y="2" width="28" height="28" rx="7" className="fill-primary" />
      <path d="M22.5 10H12.5a3 3 0 0 0 0 6h7a3 3 0 0 1 0 6H9.5" fill="none" className="stroke-primary-foreground" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** The name beside the mark, set in lowercase as the logo lockup; screen readers get "Softwarden". */
export function Wordmark({ className }: { className?: string }) {
  return (
    <span className={cn("font-display font-bold tracking-[-0.035em]", className)}>
      <span aria-hidden="true">softwarden</span><span className="sr-only">Softwarden</span>
    </span>
  );
}

/** Outline shield shown beside every "IT steward" label. */
export function StewardMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" className={cn("h-3.5 w-3.5 shrink-0", className)} aria-hidden="true">
      <path d="M8 1.8 13 3.6v3.7c0 3-2 5.4-5 6.3-3-.9-5-3.3-5-6.3V3.6z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
    </svg>
  );
}

export const chip = cva(
  "sw-chip inline-flex h-6 items-center gap-1.5 whitespace-nowrap rounded-sm border px-2 text-[12.5px] font-semibold",
  {
    variants: {
      state: {
        overdue: "border-transparent bg-state-overdue text-state-overdue-foreground",
        pending: "border-state-pending-border bg-state-pending text-state-pending-foreground",
        unreviewed: "border-state-unreviewed-border bg-state-unreviewed text-state-unreviewed-foreground",
        confirmed: "border-state-confirmed-border bg-state-confirmed text-state-confirmed-foreground",
        gap: "border-[1.5px] border-dashed border-gap-border bg-gap text-gap-foreground",
        neutral: "border-border-strong bg-muted text-foreground",
        proposed: "border-lifecycle-proposed-border bg-lifecycle-proposed text-lifecycle-proposed-foreground",
        evaluating: "border-lifecycle-evaluating-border bg-lifecycle-evaluating text-lifecycle-evaluating-foreground",
        active: "border-lifecycle-active-border bg-lifecycle-active text-lifecycle-active-foreground",
        deprecated: "border-lifecycle-deprecated-border bg-lifecycle-deprecated text-lifecycle-deprecated-foreground",
        retired: "border-lifecycle-retired-border bg-lifecycle-retired text-lifecycle-retired-foreground",
      },
    },
    defaultVariants: { state: "neutral" },
  },
);

/** For a chip whose text is free and can be long (an address, a sentence): it may wrap onto a second line inside its
 *  container instead of widening the page. Merge it after `chip()` with `cn`. */
export const CHIP_WRAP = "h-auto min-h-6 max-w-full whitespace-normal py-0.5 [overflow-wrap:anywhere]";

const STATE: Record<ReviewState, { variant: "overdue" | "pending" | "unreviewed" | "confirmed"; glyph: string; label: string }> = {
  overdue: { variant: "overdue", glyph: "!", label: "Overdue" },
  needs_confirmation: { variant: "pending", glyph: "◐", label: "Needs confirmation" },
  unreviewed: { variant: "unreviewed", glyph: "○", label: "Unreviewed" },
  confirmed: { variant: "confirmed", glyph: "✓", label: "Confirmed" },
};

export function ReviewStateChip({ state }: { state: ReviewState }) {
  const s = STATE[state];
  return <span className={chip({ state: s.variant })}><span aria-hidden="true">{s.glyph}</span>{s.label}</span>;
}

const LINK_STATE: Record<LinkState, { variant: "pending" | "unreviewed" | "confirmed"; glyph: string; label: string }> = {
  unconfirmed: { variant: "unreviewed", glyph: "○", label: "Unconfirmed" },
  needs_verification: { variant: "pending", glyph: "◐", label: "Needs verification" },
  confirmed: { variant: "confirmed", glyph: "✓", label: "Confirmed" },
};

/** Link state as a chip with a glyph and text, in the same visual language as the review state. */
export function LinkStateChip({ state }: { state: LinkState }) {
  const s = LINK_STATE[state];
  return <span className={chip({ state: s.variant })}><span aria-hidden="true">{s.glyph}</span>{s.label}</span>;
}

export function GapChip({ label }: { label: string }) {
  return <span className={chip({ state: "gap" })} data-gap=""><span aria-hidden="true">⚠</span>{label}</span>;
}

/** Chips listed side by side: wraps, and keeps 6 px between them so dashed borders never merge.
 *  Use it wherever more than one chip can sit in a row; Data flows' risk chips were the reference. */
export function ChipGroup({ className, children }: { className?: string; children: React.ReactNode }) {
  return <span className={cn("sw-chip-group inline-flex flex-wrap items-center gap-1.5", className)}>{children}</span>;
}

/** A record's gap chips, in a ChipGroup. */
export function GapChips({ gaps, className }: { gaps: readonly string[]; className?: string }) {
  return <ChipGroup className={className}>{gaps.map((g) => <GapChip key={g} label={g} />)}</ChipGroup>;
}

/** Lifecycle: an outlined chip with a glyph and the value's name. Only Deprecated carries a warning
 *  tone, so lifecycle never competes with review state. */
const LIFECYCLE: Record<string, { icon: LucideIcon; variant: "proposed" | "evaluating" | "active" | "deprecated" | "retired" }> = {
  proposed: { icon: Lightbulb, variant: "proposed" },
  evaluating: { icon: FlaskConical, variant: "evaluating" },
  active: { icon: CircleCheck, variant: "active" },
  deprecated: { icon: TriangleAlert, variant: "deprecated" },
  retired: { icon: Archive, variant: "retired" },
};

export function LifecycleChip({ value, className }: { value: string; className?: string }) {
  const l = LIFECYCLE[value];
  if (!l) return <span className={cn(chip({ state: "neutral" }), className)}>{cap(value)}</span>;
  const Icon = l.icon;
  return <span className={cn(chip({ state: l.variant }), className)}><Icon aria-hidden="true" className="size-3.5 shrink-0" strokeWidth={2} />{cap(value)}</span>;
}

/** Criticality: text plus a diamond glyph, never a filled chip, so it stays quieter than review state. Critical
 *  and High have their own colour; Medium and Low are muted. `suffix` adds words after the value (" criticality"). */
const CRITICALITY: Record<string, { tone: string; fill: "full" | "half" | "dot" | "none" }> = {
  critical: { tone: "text-criticality-critical", fill: "full" },
  high: { tone: "text-criticality-high", fill: "half" },
  medium: { tone: "text-muted-foreground", fill: "dot" },
  low: { tone: "text-muted-foreground", fill: "none" },
};

function Diamond({ fill }: { fill: "full" | "half" | "dot" | "none" }) {
  return (
    <svg viewBox="0 0 12 12" className="sw-glyph size-3 shrink-0" aria-hidden="true">
      <path d="M6 1 11 6 6 11 1 6z" fill={fill === "full" ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
      {fill === "half" && <path d="M6 1 1 6 6 11z" fill="currentColor" />}
      {fill === "dot" && <path d="M6 4 8 6 6 8 4 6z" fill="currentColor" />}
    </svg>
  );
}

export function CriticalityMark({ value, suffix = "" }: { value: string; suffix?: string }) {
  const c = CRITICALITY[value] ?? CRITICALITY.low;
  return <span className={cn("inline-flex items-center gap-1.5 font-semibold", c.tone)}><Diamond fill={c.fill} />{cap(value)}{suffix}</span>;
}

/** Record kinds: a tint and an icon, for list rows, the record header tile, the command palette and the graph.
 *  Keyed on the API's kind values (src/lib/kinds.ts), so `ai_agent` is the AI agent kind. */
export type RecordKindName = CatalogKind;
export const KIND: Record<RecordKindName, { icon: LucideIcon; label: string; tint: string }> = {
  application: { icon: KIND_ICON.application, label: KIND_LABEL.application, tint: "bg-kind-application text-kind-application-foreground" },
  component: { icon: KIND_ICON.component, label: KIND_LABEL.component, tint: "bg-kind-component text-kind-component-foreground" },
  integration: { icon: KIND_ICON.integration, label: KIND_LABEL.integration, tint: "bg-kind-integration text-kind-integration-foreground" },
  ai_agent: { icon: KIND_ICON.ai_agent, label: KIND_LABEL.ai_agent, tint: "bg-kind-agent text-kind-agent-foreground" },
};

/** The kind's icon on its tint. Decorative by default; pass `label` to have it announced. */
export function KindTile({ kind, className, label }: { kind: RecordKindName; className?: string; label?: boolean }) {
  const k = KIND[kind];
  const Icon = k.icon;
  return (
    <span className={cn("inline-grid size-6 shrink-0 place-items-center rounded-md", k.tint, className)} {...(label ? { role: "img", "aria-label": k.label } : { "aria-hidden": true })}>
      <Icon className="size-3.5" strokeWidth={2} />
    </span>
  );
}

export function Tally({ n }: { n: number }) {
  return <span className="tabular inline-grid h-5 min-w-6 place-items-center rounded-sm bg-muted px-1.5 text-xs font-semibold text-foreground">{n}</span>;
}

/** Icon tile + focusable h2 + tally + sort note (or an action), used for queues and detail cards. */
export function SectionHeading({ id, icon, title, count, note, action }: {
  id: string; icon: React.ReactNode; title: string; count?: number; note?: string; action?: React.ReactNode;
}) {
  return (
    <div className="flex min-h-[53px] flex-wrap items-center gap-2.5 border-b px-4 py-2.5">
      <span className="grid h-7 w-7 place-items-center rounded-md bg-accent text-accent-foreground" aria-hidden="true">{icon}</span>
      <h2 id={id} tabIndex={-1} className="font-display text-section font-semibold">
        {title}{count !== undefined && <span className="sr-only"> ({count})</span>}
      </h2>
      {count !== undefined && <span aria-hidden="true"><Tally n={count} /></span>}
      {note && <span className="ml-auto text-xs text-muted-foreground">{note}</span>}
      {action && <span className="ml-auto">{action}</span>}
    </div>
  );
}

/** Bordered card section headed by a SectionHeading; queues and detail cards share it. */
export function Panel({ id, icon, title, count, note, action, className, children }: {
  id: string; icon: React.ReactNode; title: string; count?: number; note?: string; action?: React.ReactNode;
  className?: string; children: React.ReactNode;
}) {
  return (
    <section className={cn("overflow-hidden rounded-lg border bg-card", className)} aria-labelledby={id}>
      <SectionHeading id={id} icon={icon} title={title} count={count} note={note} action={action} />
      {children}
    </section>
  );
}

/** Label/value rows separated by hairlines, as in the detail mockup's Record details and Ownership cards. */
export function DetailList({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <dl className={cn("grid grid-cols-[minmax(120px,max-content)_minmax(0,1fr)] gap-x-6 px-4 [&>dd]:py-2.5 [&>dt]:py-2.5 [&>dd:not(:last-child)]:border-b [&>dt:not(:nth-last-child(2))]:border-b [&>dd]:border-border-subtle [&>dt]:border-border-subtle", className)}>
      {children}
    </dl>
  );
}

export function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return <><dt className="text-[13px] text-muted-foreground">{label}</dt><dd className="min-w-0 [overflow-wrap:anywhere]">{children}</dd></>;
}

/** Feedback after a save. The status region stays mounted so the change is announced; the visible copy is aria-hidden. */
/** `quiet`: shown but not said, because the shell has already announced it (a save's note, useLandingNotice). */
export function SuccessNote({ text, quiet = false }: { text: string; quiet?: boolean }) {
  return (
    <>
      <p className="sr-only" role="status">{quiet ? "" : text}</p>
      {text && <p className="mb-4 flex items-center gap-2 rounded-md border border-success-border bg-success-bg px-3 py-2 text-success-foreground" aria-hidden="true"><span>✓</span>{text}</p>}
    </>
  );
}

function initials(name: string) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
}

export function Avatar({ name }: { name: string }) {
  return <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-avatar text-[10.5px] font-semibold text-avatar-foreground" aria-hidden="true">{initials(name)}</span>;
}

export function StewardCell({ s }: { s: Steward }) {
  const me = useMe();
  if (s.name === "Unassigned") return <span className={chip({ state: "neutral" })}>Unassigned</span>;
  const you = s.name === me.user.name;
  return (
    <span className="inline-flex items-center gap-2">
      <Avatar name={s.name} />
      <span>{s.name}{s.coordinator && <span className="text-muted-foreground"> (coordinator)</span>}</span>
      {you && <span className="rounded-sm bg-accent px-1.5 text-xs font-semibold text-accent-foreground">You</span>}
    </span>
  );
}

/** Pressed-state toggles inside a labelled group, each with its count (ScopeToggle). */
const SCOPE_LABEL = { mine: "Assigned to me", all: "All", unassigned: "Unassigned" } as const;
export function ScopeToggle<S extends keyof typeof SCOPE_LABEL>({ scope, scopes, onChange, counts }: {
  scope: S; scopes: readonly S[]; onChange: (s: S) => void; counts: Partial<Record<S, number>>;
}) {
  return (
    // Wraps rather than widening the page at 320 px, with tighter padding below `sm`.
    <div className="inline-flex max-w-full flex-wrap overflow-hidden rounded-md border border-border-strong bg-card" role="group" aria-label="Show items">
      {scopes.map((s) => (
        <button key={s} type="button" aria-pressed={scope === s} onClick={() => onChange(s)}
          className="flex h-8 items-center gap-1.5 px-2 text-[13px] sm:gap-2 sm:px-3 text-muted-foreground aria-pressed:bg-accent aria-pressed:font-semibold aria-pressed:text-accent-foreground [&+&]:border-l [&+&]:border-border-strong">
          {SCOPE_LABEL[s]}
          {counts[s] !== undefined && <Tally n={counts[s]!} />}
        </button>
      ))}
    </div>
  );
}

/** Tag colours: the API stores a hue name, mapped here to the `tag-*` tokens (tokens.json, contrast-checked
 *  in both themes). The chip always shows the tag's name; colour is a second channel. Blank or unknown is neutral. */
const TAG_TONE: Record<TagColor, string> = {
  gray: "border-tag-gray-border bg-tag-gray text-tag-gray-foreground",
  blue: "border-tag-blue-border bg-tag-blue text-tag-blue-foreground",
  teal: "border-tag-teal-border bg-tag-teal text-tag-teal-foreground",
  green: "border-tag-green-border bg-tag-green text-tag-green-foreground",
  amber: "border-tag-amber-border bg-tag-amber text-tag-amber-foreground",
  orange: "border-tag-orange-border bg-tag-orange text-tag-orange-foreground",
  red: "border-tag-red-border bg-tag-red text-tag-red-foreground",
  violet: "border-tag-violet-border bg-tag-violet text-tag-violet-foreground",
  magenta: "border-tag-magenta-border bg-tag-magenta text-tag-magenta-foreground",
};

/** A tag's name is free text. The chip is at most its container's width and a name too long for it ends in an
 *  ellipsis (the whole name stays in the text, and in the tooltip), instead of running out of a narrow cell over the
 *  next column's chip (the Applications name column is 144 px wide at 375 px). */
export function TagChip({ name, color, description }: { name: string; color?: string; description?: string }) {
  const tone = color && color in TAG_TONE ? TAG_TONE[color as TagColor] : null;
  return (
    <span className={cn(tone ? cn("sw-chip inline-flex h-6 items-center gap-1.5 whitespace-nowrap rounded-sm border px-2 text-[12.5px] font-semibold", tone) : chip({ state: "neutral" }), "max-w-full")}
      data-tag-color={tone ? color : undefined} title={description ? `${name}: ${description}` : name}>
      <span className="truncate">{name}</span>
    </span>
  );
}

/** StatTile: a label, a value in tabular numerals, and an optional line under it (a delta with its
 *  glyph and words, or what the value counts). `level`: its label's heading level, 2 where the tiles sit right under the
 *  page's h1 (headings never skip a level), 3 inside a section. */
export function StatTile({ id, label, value, detail, className, level = 3 }: {
  id: string; label: string; value: React.ReactNode; detail?: React.ReactNode; className?: string; level?: 2 | 3;
}) {
  const Heading = level === 2 ? "h2" : "h3";
  return (
    <section aria-labelledby={id} className={cn("rounded-lg border bg-card px-4 py-3", className)}>
      <Heading id={id} className="text-[13px] font-semibold text-muted-foreground">{label}</Heading>
      <p className="tabular mt-1 font-display text-title font-bold text-text-strong" data-testid={`${id}-value`}>{value}</p>
      {detail && <p className="mt-0.5 text-sm text-muted-foreground">{detail}</p>}
    </section>
  );
}

/** ChartCard: a titled card for one chart, with a one-line summary, the chart, and an optional legend in the shared
 *  style (a swatch plus words: colour is never the only key). The chart's data must also be on the page as text (a
 *  table or direct labels), so the chart itself can be decoration for screen readers. */
export function ChartCard({ id, title, summary, legend, children, className }: {
  id: string; title: string; summary?: React.ReactNode; legend?: { label: string; swatch: string }[]; children: React.ReactNode; className?: string;
}) {
  return (
    <section aria-labelledby={id} className={cn("overflow-hidden rounded-lg border bg-card", className)}>
      <div className="border-b px-4 py-2.5">
        <h3 id={id} className="font-display text-section font-semibold">{title}</h3>
        {summary && <p className="text-sm text-muted-foreground">{summary}</p>}
      </div>
      <div className="px-2 py-3">{children}</div>
      {legend && legend.length > 0 && (
        <ul aria-label="Legend" className="m-0 flex list-none flex-wrap gap-x-4 gap-y-1 border-t px-4 py-2 text-sm">
          {legend.map((l) => <li key={l.label} className="inline-flex items-center gap-1.5"><span aria-hidden="true" className={cn("inline-block size-3 rounded-sm", l.swatch)} />{l.label}</li>)}
        </ul>
      )}
    </section>
  );
}
