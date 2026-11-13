/** CommandPalette on cmdk, opened with Ctrl+K / Cmd+K, `/` (on pages without
 *  a search box) or the top bar's Search button. Loaded on first use (React.lazy in App.tsx), so cmdk stays out of the
 *  first bundle.
 *
 *  Groups: Recent (the last records opened from here, per user and organisation, in this browser), results by kind
 *  (applications, components, AI agents, integrations, vendors, people, attachments, contracts, renewals), Pages, Actions. Results
 *  come from global search (`GET search`: names, descriptions, vendors, tags, models and text custom fields,
 *  with a snippet where the match wasn't the name) and, for renewals, which search doesn't cover yet, the renewals
 *  list's `q`; both 250 ms after typing stops, cancelled when the palette closes or the search changes. "Search
 *  everything for ..." opens the results page. Viewers get no create actions.
 *
 *  Recent keeps only each record's kind and ID, never its name (nothing personal stays in the browser), and no
 *  people; names are read from the API when the palette shows them, and a record that can't be read any more (deleted,
 *  or no longer visible) is dropped. */
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueries, useQuery } from "@tanstack/react-query";
import { Command } from "cmdk";
import { CalendarClock, Clock, Moon, Plus, Repeat, Search, Sun, Keyboard, type LucideIcon } from "lucide-react";
import { api, Problem, type Me, type Membership, type Renewal, type SearchResponse, type SearchResult, type SearchSegment } from "@/api";
import { OTHER_ICON, resultHref, retryText, Segments } from "@/features/search/results";
import { useReturnFocus } from "@/components/app";
import { createActionsFor, navFor } from "@/components/nav";
import { KindTile, type RecordKindName } from "@/components/sw";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { apiPath, KIND_LABEL, recordPath } from "@/lib/kinds";
import { prefKey, usePref } from "@/lib/prefs";
import { apiKey } from "@/lib/query";
import { setThemePref, useTheme } from "@/lib/theme";

/** What Recent stores: a record's kind and ID only. */
type RecentRef = { kind: "application" | "component" | "ai_agent" | "integration" | "renewal" | "contract"; id: string };
const RECENT_PATH: Record<RecentRef["kind"], string> = {
  application: apiPath("application"), component: apiPath("component"), ai_agent: apiPath("ai_agent"), integration: apiPath("integration"), renewal: "renewals",
  contract: "contracts",
};
const recentTo = (r: RecentRef) => (r.kind === "renewal" ? `/renewals/${r.id}` : r.kind === "contract" ? `/contracts/${r.id}` : recordPath(r.kind, r.id)!);
type RecordHit = {
  kind: RecentRef["kind"] | "person" | "vendor" | "attachment"; id: string; name: string; hint?: string; to: string;
  snippet?: { label: string; segments: SearchSegment[] };
};
const MAX_RECENT = 8;
const PER_KIND = 5;
const DEBOUNCE_MS = 250;

const KIND_GROUPS: { kind: RecordHit["kind"]; heading: string }[] = [
  { kind: "application", heading: "Applications" },
  { kind: "component", heading: "Components" },
  { kind: "ai_agent", heading: "AI agents" },
  { kind: "integration", heading: "Integrations" },
  { kind: "vendor", heading: "Vendors" },
  { kind: "person", heading: "People" },
  { kind: "attachment", heading: "Attachments" },
  { kind: "contract", heading: "Contracts" },
  { kind: "renewal", heading: "Renewals" },
];

function useDebounced(value: string, ms: number) {
  const [v, setV] = useState(value);
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t); }, [value, ms]);
  return v;
}

/** One list endpoint searched with `q`; cancelled through TanStack Query's signal when nobody shows it any more. */
function useSearch<T>(path: string | null) {
  return useQuery<T>({
    queryKey: apiKey(path ?? "palette-idle"),
    queryFn: ({ signal }) => api<T>(path!, { signal }),
    enabled: path !== null,
    staleTime: 30_000,
  });
}

export default function CommandPalette({ open, onOpenChange, me, initialSearch, onSwitchOrganization, onShowShortcuts }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  me: Me;
  initialSearch: string;
  onSwitchOrganization: (org: Membership) => void;
  onShowShortcuts: () => void;
}) {
  const returnFocus = useReturnFocus(open);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent showCloseButton={false} {...returnFocus}
        className="bottom-auto top-[12vh] my-0 gap-0 overflow-hidden p-0 sm:max-w-xl">
        <DialogTitle className="sr-only">Command palette</DialogTitle>
        <DialogDescription className="sr-only">Search records, go to a page or run an action. Arrow keys move, Enter opens, Esc closes.</DialogDescription>
        {open && <PaletteBody me={me} initialSearch={initialSearch} close={() => onOpenChange(false)}
          onSwitchOrganization={onSwitchOrganization} onShowShortcuts={onShowShortcuts} />}
      </DialogContent>
    </Dialog>
  );
}

function PaletteBody({ me, initialSearch, close, onSwitchOrganization, onShowShortcuts }: {
  me: Me; initialSearch: string; close: () => void; onSwitchOrganization: (org: Membership) => void; onShowShortcuts: () => void;
}) {
  const navigate = useNavigate();
  const { theme } = useTheme();
  const [search, setSearch] = useState(initialSearch);
  const term = useDebounced(search.trim(), DEBOUNCE_MS);
  const searching = term.length >= 2;
  const q = encodeURIComponent(term);
  const [stored, setRecent] = usePref<RecentRef[]>(prefKey(me.user.id, `palette.recent.${me.organization.id}`), []);
  // Older entries may carry more than kind and ID, or be people: keep only the reference to a record.
  const refs = stored.filter((r) => r && r.kind in RECENT_PATH && typeof r.id === "string").map((r) => ({ kind: r.kind, id: r.id }));
  const resolved = useQueries({
    queries: refs.map((r) => {
      const path = `${RECENT_PATH[r.kind]}/${r.id}`;
      return { queryKey: apiKey(path), queryFn: () => api<{ name: string }>(path), staleTime: 30_000, retry: false };
    }),
  });
  const recent: RecordHit[] = refs.flatMap((r, i) => {
    const name = resolved[i]?.data?.name;
    return name ? [{ kind: r.kind, id: r.id, name, to: recentTo(r) }] : [];
  });
  // Drop what can't be read any more (deleted, or another organisation's), so it doesn't come back.
  const gone = refs.filter((_, i) => resolved[i]?.error instanceof Problem && [403, 404].includes((resolved[i].error as Problem).status));
  const goneKey = gone.map((g) => g.id).join(",");
  useEffect(() => {
    if (gone.length) setRecent(refs.filter((r) => !gone.some((g) => g.id === r.id)));
  }, [goneKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const found = useSearch<SearchResponse>(searching ? `search?q=${q}&limit=${PER_KIND}` : null);
  const renewals = useSearch<Renewal[]>(searching && me.capabilities.includes("renewals") ? `renewals?q=${q}` : null);
  const queries = [found, renewals];
  const loading = searching && queries.some((r) => r.isFetching);
  const failure = searching ? (found.error ?? renewals.error) : null;
  // A query with nothing searchable in it (400) isn't an error here: pages and actions still match.
  const searchError = failure instanceof Problem && failure.status === 400 ? ""
    : failure instanceof Problem && failure.status === 429 ? retryText(failure.retryAfter)
      : failure ? "Some results couldn't be loaded. Try again." : "";

  const hits = useMemo<RecordHit[]>(() => {
    if (!searching) return [];
    const fromSearch = (found.data?.groups ?? []).flatMap((g) => g.results).flatMap((r: SearchResult): RecordHit[] => {
      const to = resultHref(r);
      if (!to) return [];
      const hint = r.parent ? `on ${r.parent.name}` : r.archived ? (r.kind === "person" ? "inactive" : "archived") : undefined;
      return [{ kind: r.kind, id: r.id, name: r.name, hint, to, snippet: r.snippet ?? undefined }];
    });
    return [
      ...fromSearch,
      ...(renewals.data ?? []).slice(0, PER_KIND).map((r) => ({ kind: "renewal" as const, id: r.id, name: r.name, hint: r.vendor?.name, to: `/renewals/${r.id}` })),
    ];
  }, [searching, found.data, renewals.data]);

  const needle = search.trim().toLowerCase();
  const matches = (label: string) => !needle || label.toLowerCase().includes(needle);
  const pages = navFor(me).filter((n) => matches(n.label) || matches(`go to ${n.label}`));
  const others = (me.organizations ?? []).filter((o) => o.id !== me.organization.id);
  const actions: { id: string; label: string; icon: LucideIcon; run: () => void }[] = [
    ...(me.role !== "viewer" ? createActionsFor(me).map((a) => ({ id: a.id, label: a.label, icon: Plus, run: () => navigate(a.to) })) : []),
    { id: "theme", label: theme === "dark" ? "Switch to the light theme" : "Switch to the dark theme", icon: theme === "dark" ? Sun : Moon, run: () => setThemePref(theme === "dark" ? "light" : "dark") },
    ...others.map((o) => ({ id: `org-${o.id}`, label: `Switch organization to ${o.name}`, icon: Repeat, run: () => onSwitchOrganization(o) })),
    { id: "shortcuts", label: "Show keyboard shortcuts", icon: Keyboard, run: onShowShortcuts },
  ].filter((a) => matches(a.label));
  const shownRecent = needle ? [] : recent;

  function openRecord(hit: RecordHit) {
    if (hit.kind !== "person" && hit.kind !== "vendor" && hit.kind !== "attachment") {
      const ref: RecentRef = { kind: hit.kind, id: hit.id };
      setRecent([ref, ...refs.filter((r) => !(r.kind === ref.kind && r.id === ref.id))].slice(0, MAX_RECENT));
    }
    close();
    navigate(hit.to);
  }
  function run(fn: () => void) {
    close();
    // After the dialog has closed and returned focus, so a dialog the action opens returns focus there too.
    setTimeout(fn);
  }
  const everything = searching ? `/search?q=${q}` : null;
  const nothing = !loading && !everything && hits.length === 0 && pages.length === 0 && actions.length === 0 && shownRecent.length === 0;

  // The highlighted item. Filtering is ours (server search), so keep the first item highlighted as results arrive:
  // Enter then opens the best match, as it would with cmdk's own filtering.
  const firstHit = KIND_GROUPS.map((g) => hits.find((h) => h.kind === g.kind)).find(Boolean);
  const first = shownRecent[0] ? `recent-${shownRecent[0].kind}-${shownRecent[0].id}`
    : firstHit ? `${firstHit.kind}-${firstHit.id}` : pages[0] ? `page-${pages[0].id}` : actions[0] ? `action-${actions[0].id}` : everything ? "search-everything" : "";
  const [highlighted, setHighlighted] = useState(first);
  useEffect(() => { setHighlighted(first); }, [first]);

  return (
    // cmdk names its input from `label` (aria-labelledby), so the label is the input's name.
    <Command label="Search records, pages and actions" shouldFilter={false} loop value={highlighted} onValueChange={setHighlighted} className="flex flex-col">
      <div className="flex items-center gap-2 border-b px-3">
        <Search aria-hidden="true" className="size-4 text-icon" />
        <Command.Input value={search} onValueChange={setSearch} placeholder="Search records, pages and actions"
          className="h-12 w-full bg-transparent text-base outline-none placeholder:text-muted-foreground md:text-sm" />
      </div>
      <Command.List className="max-h-[min(60vh,28rem)] overflow-y-auto p-1.5">
        {nothing && <p className="px-3 py-6 text-center text-muted-foreground">Nothing matches “{search.trim()}”. Try part of a name.</p>}
        {searching && loading && <Command.Loading><p className="px-3 py-2 text-muted-foreground">Searching…</p></Command.Loading>}
        {searchError && <p role="alert" className="px-3 py-2 text-error-foreground">{searchError}</p>}
        {searching && found.data?.fuzzy && hits.length > 0 && <p className="px-3 py-1.5 text-xs text-muted-foreground" data-testid="palette-fuzzy">Showing close matches: nothing matched every word.</p>}
        {searching && found.data && found.data.ignored.length > 0 && <p className="px-3 py-1.5 text-xs text-muted-foreground">Ignored: {found.data.ignored.join(" ")}</p>}
        {shownRecent.length > 0 && (
          <Group heading="Recent">
            {shownRecent.map((r) => <RecordItem key={`recent-${r.kind}-${r.id}`} value={`recent-${r.kind}-${r.id}`} hit={r} recent onSelect={() => openRecord(r)} />)}
          </Group>
        )}
        {KIND_GROUPS.map(({ kind, heading }) => {
          const list = hits.filter((h) => h.kind === kind);
          if (!list.length) return null;
          return (
            <Group key={kind} heading={heading}>
              {list.map((h) => <RecordItem key={`${kind}-${h.id}`} value={`${kind}-${h.id}`} hit={h} onSelect={() => openRecord(h)} />)}
            </Group>
          );
        })}
        {everything && (
          <Group heading="Search">
            <Item value="search-everything" onSelect={() => { close(); navigate(everything); }}>
              <IconTile icon={Search} /><span className="min-w-0 flex-1 truncate">Search everything for “{term}”</span><Hint>All results</Hint>
            </Item>
          </Group>
        )}
        {pages.length > 0 && (
          <Group heading="Pages">
            {pages.map((p) => (
              <Item key={p.id} value={`page-${p.id}`} onSelect={() => { close(); navigate(p.to); }}>
                <IconTile icon={p.icon} /><span className="min-w-0 flex-1">{p.label}</span>
                {p.key && <Hint>g {p.key}</Hint>}
              </Item>
            ))}
          </Group>
        )}
        {actions.length > 0 && (
          <Group heading="Actions">
            {actions.map((a) => (
              <Item key={a.id} value={`action-${a.id}`} onSelect={() => run(a.run)}>
                <IconTile icon={a.icon} /><span className="min-w-0 flex-1">{a.label}</span>
              </Item>
            ))}
          </Group>
        )}
      </Command.List>
      <p className="flex flex-wrap gap-x-4 gap-y-1 border-t px-3 py-2 text-xs text-muted-foreground" aria-hidden="true">
        <span><Kbd>↑</Kbd> <Kbd>↓</Kbd> move</span><span><Kbd>Enter</Kbd> open</span><span><Kbd>Esc</Kbd> close</span>
        <span className="ml-auto"><Kbd>?</Kbd> all shortcuts</span>
      </p>
    </Command>
  );
}

function Group({ heading, children }: { heading: string; children: React.ReactNode }) {
  return (
    <Command.Group heading={heading}
      className="mb-1 [&_[cmdk-group-heading]]:px-2.5 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:font-semibold [&_[cmdk-group-heading]]:text-muted-foreground">
      {children}
    </Command.Group>
  );
}

function Item({ value, onSelect, children }: { value: string; onSelect: () => void; children: React.ReactNode }) {
  return (
    <Command.Item value={value} onSelect={onSelect}
      className="flex min-h-10 cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm data-[selected=true]:bg-accent data-[selected=true]:text-accent-foreground">
      {children}
    </Command.Item>
  );
}

const KIND_ICON: Partial<Record<RecordHit["kind"], LucideIcon>> = { ...OTHER_ICON, renewal: CalendarClock };
const HIT_LABEL: Record<RecordHit["kind"], string> = { ...KIND_LABEL, person: "Person", vendor: "Vendor", attachment: "Attachment", renewal: "Renewal", contract: "Contract" };

function RecordItem({ value, hit, recent, onSelect }: { value: string; hit: RecordHit; recent?: boolean; onSelect: () => void }) {
  const icon = KIND_ICON[hit.kind];
  return (
    <Item value={value} onSelect={onSelect}>
      {icon ? <IconTile icon={recent ? Clock : icon} /> : <KindTile kind={hit.kind as RecordKindName} />}
      <span className="min-w-0 flex-1">
        <span className="block truncate">{hit.name}{hit.hint && <span className="text-muted-foreground"> · {hit.hint}</span>}</span>
        {hit.snippet && <span className="block truncate text-xs text-muted-foreground" data-testid="search-snippet">{hit.snippet.label}: <Segments segments={hit.snippet.segments} /></span>}
      </span>
      <Hint>{HIT_LABEL[hit.kind]}</Hint>
    </Item>
  );
}

function IconTile({ icon: Icon }: { icon: LucideIcon }) {
  return <span className="inline-grid size-6 shrink-0 place-items-center rounded-md bg-muted text-icon" aria-hidden="true"><Icon className="size-3.5" /></span>;
}

function Hint({ children }: { children: React.ReactNode }) {
  return <span className="shrink-0 text-xs text-muted-foreground">{children}</span>;
}

function Kbd({ children }: { children: React.ReactNode }) {
  return <kbd className="rounded-sm border border-border-strong bg-muted px-1 font-mono text-[11px] text-foreground">{children}</kbd>;
}
