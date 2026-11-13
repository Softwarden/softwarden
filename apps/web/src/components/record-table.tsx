/** RecordTable: one table for the cursor-paginated record lists, on TanStack
 *  Table (column visibility, row selection and manual sorting) and TanStack Query.
 *
 *  - Server-side sort through `ordering` (only the endpoint's whitelisted fields: a column sorts when it names a
 *    `sortField`), server-side filters (the caller's query), keyset pagination (`next` / `previous` cursors and
 *    `page_size`). Sort, page and page size live in the URL (`ordering`, `cursor`, `page`, `page_size`), beside the
 *    caller's filters; changing a filter or the sort goes back to the first page.
 *  - Per-user column choice and density, kept in this browser (src/lib/prefs.ts) under the user's ID and the table's
 *    ID. The row header column (the record's name) can't be hidden.
 *  - Row selection for bulk actions (checkboxes, Editors and Admins only): the caller renders `<BulkBar>` in place of
 *    its page header actions while rows are selected. Selection is for the page on screen and clears when the page,
 *    sort or filters change. Selection changes are announced in a live region.
 *  - Accessible: a caption naming the order, `aria-sort` on the `th` of each sortable column, sort buttons inside the
 *    headers, the name column as the row header (sticky on the left from 640 px; narrower below so the other columns
 *    keep room at 320 px and at 200 % zoom), a labelled focusable scroll region.
 *
 *  Usage: `const table = useRecordTable({ id, path, query, max })`, then `<RecordTable table={table} … />` and
 *  `{table.selected.size > 0 && <BulkBar table={table}>…</BulkBar>}`. */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { columnVisibilityFeature, rowSelectionFeature, rowSortingFeature, tableFeatures, useTable, type ColumnDef, type RowSelectionState, type SortingState, type Updater } from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ArrowUpDown, Columns3, Rows3, Rows4 } from "lucide-react";
import { Page, Problem } from "@/api";
import { ErrorBanner } from "@/components/app";
import { KindTile, type RecordKindName } from "@/components/sw";
import { Button, buttonVariants } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { problemText, useApiQuery } from "@/lib/query";
import { prefKey, usePref } from "@/lib/prefs";
import { useMe } from "@/lib/session";
import { cn } from "@/lib/utils";

export type Density = "comfortable" | "compact";
export const PAGE_SIZES = [25, 50, 100, 200] as const;
const DEFAULT_PAGE_SIZE = 50;
const MAX_PAGE_SIZE = 200;
/** URL parameters the table owns. Everything else in the URL is the caller's (filters, kind, tabs). */
export const TABLE_PARAMS = ["ordering", "cursor", "page", "page_size"] as const;

export type RecordColumn<T> = {
  id: string;
  header: string;
  cell: (row: T) => React.ReactNode;
  /** The API's `ordering` field: makes the header a sort button. Only fields in the endpoint's whitelist. */
  sortField?: string;
  /** How the order reads for this sort field, ascending then descending (default "A to Z", "Z to A"). */
  sortText?: [string, string];
  /** Shown until the user changes the column choice. Default true. */
  defaultVisible?: boolean;
  className?: string;
};

/** A row: its ID, and the name it is listed by (`name`, unless the table says otherwise with `nameOf`). */
type Row = { id: string; name?: string };
export type ColumnPref = { hidden: string[]; known: string[] | null };

/** The IDs of the columns that show: the user's stored choice for the columns it knew, each other column's default.
 *  The row header always shows. Saved views compare this with a view's columns. */
export function visibleColumnIds<T>(columns: RecordColumn<T>[], hidden: ColumnPref | null, rowHeader: string): string[] {
  const byDefault = (c: RecordColumn<T>) => c.defaultVisible !== false;
  const known = (c: RecordColumn<T>) => (hidden?.known ? hidden.known.includes(c.id) : !c.id.startsWith("cf_"));
  const on = (c: RecordColumn<T>) => (hidden && known(c) ? !hidden.hidden.includes(c.id) : byDefault(c));
  return columns.filter((c) => c.id === rowHeader || on(c)).map((c) => c.id);
}

const features = tableFeatures({ columnVisibilityFeature, rowSelectionFeature, rowSortingFeature });

/** The table's state: the list query (TanStack Query), URL state, selection and the user's display choices. */
export function useRecordTable<T extends Row>({ id, path, query, maxSelection = 200 }: {
  /** Names the table in stored preferences ("applications"). */
  id: string;
  /** The list endpoint relative to /api/v1/ ("applications"). */
  path: string;
  /** The filters, as the API's query parameters. */
  query: Record<string, string>;
  maxSelection?: number;
}) {
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const ordering = params.get("ordering") ?? "";
  const cursor = params.get("cursor") ?? "";
  const pageNumber = Number(params.get("page")) || 1;
  const sizeParam = Number(params.get("page_size"));
  // Any whole number the API accepts (1 to 200) works from a shared link; the menu offers the usual sizes.
  const pageSize = Number.isInteger(sizeParam) && sizeParam >= 1 && sizeParam <= MAX_PAGE_SIZE ? sizeParam : DEFAULT_PAGE_SIZE;

  const filterString = new URLSearchParams(Object.entries(query).filter(([, v]) => v !== "")).toString();
  const request = new URLSearchParams(filterString);
  if (ordering) request.set("ordering", ordering);
  if (cursor) request.set("cursor", cursor);
  if (pageSize !== DEFAULT_PAGE_SIZE) request.set("page_size", String(pageSize));
  const apiPath = `${path}?${request.toString()}`;
  const list = useApiQuery<Page<T>>(apiPath);

  /** Change the table's URL parameters; `null` removes one. Replaces the history entry, as the filters do. */
  function setTableParams(changes: Partial<Record<(typeof TABLE_PARAMS)[number], string | null>>) {
    setParams((cur) => {
      const next = new URLSearchParams(cur);
      for (const [k, v] of Object.entries(changes)) {
        if (v === null || v === "") next.delete(k);
        else next.set(k, v);
      }
      return next;
    }, { replace: true });
  }
  const firstPage = { cursor: null, page: null };
  const setOrdering = (o: string) => setTableParams({ ordering: o || null, ...firstPage });
  const setPageSize = (n: number) => setTableParams({ page_size: n === DEFAULT_PAGE_SIZE ? null : String(n), ...firstPage });
  const goTo = (direction: "next" | "previous") => {
    const target = direction === "next" ? list.data?.next : list.data?.previous;
    if (!target) return;
    const page = direction === "next" ? pageNumber + 1 : Math.max(1, pageNumber - 1);
    setTableParams({ cursor: page === 1 ? null : target, page: page === 1 ? null : String(page) });
  };

  // A filter change outside the table (search, chips, presets) starts again at the first page: a cursor only works
  // with the query and ordering it came from.
  const filterKey = `${path}?${filterString}`;
  const lastFilter = useRef(filterKey);
  useEffect(() => {
    if (lastFilter.current === filterKey) return;
    lastFilter.current = filterKey;
    if (params.get("cursor") || params.get("page")) setTableParams(firstPage);
  });

  // Selection is for the rows on screen: it clears when anything about the request changes.
  const [selection, setSelection] = useState<RowSelectionState>({});
  useEffect(() => { setSelection({}); }, [apiPath]);
  const selected = useMemo(() => new Set(Object.keys(selection).filter((k) => selection[k])), [selection]);

  // Display choices per user and table: hidden columns (by column ID) and density. Stored with the columns the
  // table had when the choice was made (`known`), so a column added later (a new custom field) starts as its default
  // says rather than shown because it isn't in the hidden list. An older plain list counts every column but custom
  // field ones (`cf_`) as known.
  const [columnPref, setColumnPref] = usePref<ColumnPref | string[] | null>(prefKey(me.user.id, `table.${id}.columns`), null);
  const hidden: ColumnPref | null = columnPref === null ? null : Array.isArray(columnPref) ? { hidden: columnPref, known: null } : columnPref;
  const setHidden = (h: string[] | null, known: string[] = []) => setColumnPref(h === null ? null : { hidden: h, known });
  const [density, setDensity] = usePref<Density>(prefKey(me.user.id, `table.${id}.density`), "comfortable");

  const [announcement, setAnnouncement] = useState("");
  const rows = list.data?.results ?? null;

  return {
    id, path, apiPath, list, rows, ordering, cursor, pageNumber, pageSize, maxSelection,
    next: list.data?.next ?? null, previous: list.data?.previous ?? null,
    setOrdering, setPageSize, goTo,
    selection, setSelection, selected, clearSelection: () => setSelection({}),
    hidden, setHidden, density, setDensity,
    announcement, announce: setAnnouncement,
  };
}

export type RecordTableState<T extends Row> = ReturnType<typeof useRecordTable<T>>;

/** "name, A to Z" for `ordering=name`; the default order is described by the caller. */
function orderText(ordering: string, columns: RecordColumn<unknown>[]) {
  const desc = ordering.startsWith("-");
  const field = ordering.replace(/^-/, "");
  const col = columns.find((c) => c.sortField === field);
  const [asc, dsc] = col?.sortText ?? ["A to Z", "Z to A"];
  return `${(col?.header ?? field.replace(/_/g, " ")).toLowerCase()}, ${desc ? dsc : asc}`;
}

export function RecordTable<T extends Row>({ table, caption, noun, columns, rowHeader, kind, rowHref, selectable, defaultOrder, empty, onClearFilters, nameOf = (r) => r.name ?? "" }: {
  table: RecordTableState<T>;
  /** The table's name: "Applications". Also names the scroll region ("Applications table"). */
  caption: string;
  /** Singular and plural nouns for counts: ["application", "applications"]. */
  noun: [string, string];
  columns: RecordColumn<T>[];
  /** The column that names the row (a `th scope="row"`, sticky, never hidden, with the kind icon and the link). */
  rowHeader: string;
  kind?: RecordKindName;
  rowHref: (row: T) => string;
  /** Show row checkboxes (Editors and Admins). */
  selectable: boolean;
  /** How rows are ordered without `ordering`, as words, and which sort field that matches (null: by relevance). */
  defaultOrder: { text: string; field: string | null };
  /** Shown instead of the table when the page has no rows. */
  empty: React.ReactNode;
  /** A 400 (a filter the list refuses, such as a stale custom field filter) offers this instead of Retry. */
  onClearFilters?: () => void;
  /** The row's name, for its link and checkbox (default `name`; licence pools are listed by their SKU name). */
  nameOf?: (row: T) => string;
}) {
  const { list, rows, ordering, hidden, density, selection, maxSelection } = table;
  const sorting: SortingState = useMemo(() => {
    if (ordering) return [{ id: ordering.replace(/^-/, ""), desc: ordering.startsWith("-") }];
    return defaultOrder.field ? [{ id: defaultOrder.field, desc: false }] : [];
  }, [ordering, defaultOrder.field]);

  const columnDefs = useMemo<ColumnDef<typeof features, T>[]>(() => columns.map((c) => ({
    // Sortable columns use the API field as their ID, so TanStack's sorting state is the `ordering` parameter.
    id: c.sortField ?? c.id,
    header: c.header,
    enableSorting: !!c.sortField,
    enableHiding: c.id !== rowHeader,
    cell: ({ row }) => c.cell(row.original),
  })), [columns, rowHeader]);
  const byDefId = useMemo(() => new Map(columns.map((c) => [c.sortField ?? c.id, c])), [columns]);
  const columnVisibility = useMemo(() => {
    const shown = new Set(visibleColumnIds(columns, hidden, rowHeader));
    return Object.fromEntries(columns.map((c) => [c.sortField ?? c.id, shown.has(c.id)]));
  }, [hidden, columns, rowHeader]);

  const data = useMemo(() => rows ?? [], [rows]);
  const t = useTable({
    features,
    columns: columnDefs,
    data,
    getRowId: (r: T) => r.id,
    manualSorting: true,
    enableMultiSort: false,
    enableRowSelection: (row) => selection[row.id] || Object.values(selection).filter(Boolean).length < maxSelection,
    state: { sorting, columnVisibility, rowSelection: selection },
    onSortingChange: (u: Updater<SortingState>) => {
      const next = typeof u === "function" ? u(sorting) : u;
      table.setOrdering(next.length ? `${next[0].desc ? "-" : ""}${next[0].id}` : "");
    },
    onRowSelectionChange: (u: Updater<RowSelectionState>) => table.setSelection(typeof u === "function" ? u(selection) : u),
    onColumnVisibilityChange: (u) => {
      const next = typeof u === "function" ? u(columnVisibility) : u;
      table.setHidden(columns.filter((c) => c.id !== rowHeader && next[c.sortField ?? c.id] === false).map((c) => c.id), columns.map((c) => c.id));
    },
  });

  // Announce selection changes: "2 selected. Bulk actions are above the table."
  const count = table.selected.size;
  const lastCount = useRef(0);
  useEffect(() => {
    if (count === lastCount.current) return;
    const first = lastCount.current === 0; // say where the bar is once, when it appears
    lastCount.current = count;
    table.announce(count ? `${count} selected${count >= maxSelection ? `, the most you can edit at once` : ""}${first ? ". Bulk actions are above the table." : "."}` : "Selection cleared.");
  }, [count]); // eslint-disable-line react-hooks/exhaustive-deps

  const sortedBy = ordering ? orderText(ordering, columns as RecordColumn<unknown>[]) : defaultOrder.text;
  const n = rows?.length ?? 0;
  // Select all works on the row IDs on screen (up to the bulk limit), from our own selection state.
  const pageIds = (rows ?? []).slice(0, maxSelection).map((r) => r.id);
  const pageAll = pageIds.length > 0 && pageIds.every((rid) => selection[rid]);
  const pageSome = !pageAll && pageIds.some((rid) => selection[rid]);
  const rowHeight = density === "compact" ? "h-(--row-compact)" : "h-(--row-comfortable)";

  return (
    <div>
      <p className="sr-only" aria-live="polite" data-testid="table-announcer">{table.announcement}</p>
      <div className="mb-2 flex flex-wrap items-center justify-end gap-2">
        <ColumnPicker table={table} columns={columns} rowHeader={rowHeader} visibility={columnVisibility} />
        <DensityToggle density={density} onChange={table.setDensity} />
      </div>
      {list.isError && (list.error instanceof Problem && list.error.status === 400 && onClearFilters
        ? <FilterProblem problem={list.error} noun={noun[1]} onClear={onClearFilters} />
        : <ErrorBanner message={`${problemText(list.error, `Couldn't load ${noun[1]}.`)} Nothing was changed.`} onRetry={() => void list.refetch()} />)}
      {list.isPending && <TableSkeleton what={noun[1]} />}
      {rows && rows.length === 0 && empty}
      {rows && rows.length > 0 && (
        <div className="relative overflow-x-auto rounded-lg border bg-card" role="region" aria-label={`${caption} table`} tabIndex={0} aria-busy={list.isFetching || undefined}>
          {/* A bare <table>, not ui/Table: that wraps the table in its own scroll box, and the labelled, focusable region
              must be the one that scrolls (keyboard users scroll it; the sticky name column sticks to it). */}
          <table data-slot="table" className="w-full caption-bottom text-sm">
            <TableCaption className="sr-only">{caption}, sorted by {sortedBy}{selectable ? ". Select rows with the checkboxes to edit them together." : ""}</TableCaption>
            <TableHeader>
              <TableRow>
                {selectable && (
                  <TableHead scope="col" className="w-10 bg-table-head sm:sticky sm:left-0 sm:z-10">
                    <input type="checkbox" aria-label="Select all on this page" checked={pageAll}
                      ref={(el) => { if (el) el.indeterminate = pageSome && !pageAll; }}
                      onChange={() => table.setSelection(pageAll ? {} : Object.fromEntries(pageIds.map((rid) => [rid, true])))} />
                  </TableHead>
                )}
                {t.getHeaderGroups()[0].headers.map((h) => {
                  const col = byDefId.get(h.column.id)!;
                  const sorted = h.column.getIsSorted();
                  const isHeader = col.id === rowHeader;
                  return (
                    <TableHead key={h.id} scope="col" aria-sort={col.sortField ? (sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : "none") : undefined}
                      className={cn(isHeader && cn("bg-table-head sm:sticky sm:z-10", selectable ? "sm:left-10" : "sm:left-0"), col.className)}>
                      {col.sortField ? (
                        <button type="button" onClick={() => h.column.toggleSorting(sorted === "asc")}
                          className="-mx-1 inline-flex h-7 items-center gap-1 rounded-sm px-1 font-semibold text-muted-foreground hover:text-foreground">
                          {col.header}
                          {sorted === "asc" ? <ArrowUp aria-hidden="true" className="size-3.5" /> : sorted === "desc" ? <ArrowDown aria-hidden="true" className="size-3.5" /> : <ArrowUpDown aria-hidden="true" className="size-3.5 opacity-60" />}
                        </button>
                      ) : col.header}
                    </TableHead>
                  );
                })}
              </TableRow>
            </TableHeader>
            <TableBody>
              {t.getRowModel().rows.map((row) => {
                const isSelected = row.getIsSelected();
                return (
                  <TableRow key={row.id} className={cn("group/row", rowHeight)} data-state={isSelected ? "selected" : undefined}>
                    {selectable && (
                      <TableCell className="bg-card group-hover/row:bg-row-hover group-data-[state=selected]/row:bg-row-selected sm:sticky sm:left-0 sm:z-10">
                        <input type="checkbox" aria-label={`Select ${nameOf(row.original)}`} checked={isSelected}
                          disabled={!row.getCanSelect()} onChange={() => row.toggleSelected()} />
                      </TableCell>
                    )}
                    {row.getVisibleCells().map((cell) => {
                      const col = byDefId.get(cell.column.id)!;
                      if (col.id !== rowHeader) return <TableCell key={cell.id} className={col.className}><t.FlexRender cell={cell} /></TableCell>;
                      return (
                        <TableHead key={cell.id} scope="row"
                          className={cn("min-w-32 whitespace-normal bg-card font-semibold sm:sticky sm:z-10 sm:min-w-48 text-foreground [overflow-wrap:anywhere] group-hover/row:bg-row-hover group-data-[state=selected]/row:bg-row-selected",
                            selectable ? "sm:left-10" : "sm:left-0", col.className)}>
                          {/* The cap is on the content: browsers ignore max-width on table cells. */}
                          <span className="flex max-w-36 items-start gap-2 sm:max-w-xs">
                            {kind && <KindTile kind={kind} className="mt-px" />}
                            <span className="min-w-0">
                              <Link to={rowHref(row.original)}>{nameOf(row.original)}</Link>
                              {col.cell(row.original)}
                            </span>
                          </span>
                        </TableHead>
                      );
                    })}
                  </TableRow>
                );
              })}
            </TableBody>
          </table>
        </div>
      )}
      {rows && (rows.length > 0 || table.cursor) && (
        <TableFooter table={table} noun={noun} count={n} sortedBy={sortedBy} />
      )}
    </div>
  );
}

function TableFooter<T extends Row>({ table, noun, count, sortedBy }: { table: RecordTableState<T>; noun: [string, string]; count: number; sortedBy: string }) {
  const { pageNumber, next, previous, pageSize, list } = table;
  const known = !table.cursor || pageNumber > 1;
  const from = (pageNumber - 1) * pageSize + 1;
  return (
    <nav aria-label="Pagination" className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
      <p className="text-sm text-muted-foreground">
        {count === 0 ? `No ${noun[1]} on this page.` : known && (pageNumber > 1 || next)
          ? `Showing ${noun[1]} ${from}–${from + count - 1}${next ? "" : ` of ${from + count - 1}`}.`
          : `Showing ${count} ${count === 1 ? noun[0] : noun[1]}${next ? ". More available" : ""}.`}
        {" "}Sorted by {sortedBy}.
      </p>
      <div className="ml-auto flex flex-wrap items-center gap-2">
        <Label htmlFor={`${table.id}-page-size`} className="font-normal text-muted-foreground">Rows per page</Label>
        <select id={`${table.id}-page-size`} className="native-select h-8 w-20" value={pageSize} onChange={(e) => table.setPageSize(Number(e.target.value))}>
          {PAGE_SIZES.map((s) => <option key={s} value={s}>{s}</option>)}
          {!(PAGE_SIZES as readonly number[]).includes(pageSize) && <option value={pageSize}>{pageSize}</option>}
        </select>
        <Button variant="outline" size="sm" disabled={!previous || list.isFetching} onClick={() => table.goTo("previous")}>Previous page</Button>
        {known && <span className="tabular text-sm text-muted-foreground">Page {pageNumber}</span>}
        <Button variant="outline" size="sm" disabled={!next || list.isFetching} onClick={() => table.goTo("next")}>Next page</Button>
      </div>
    </nav>
  );
}

/** The list refused its filters (400): what it said about each parameter, and Clear filters. Retrying the same query
 *  would only fail again. */
function FilterProblem({ problem, noun, onClear }: { problem: Problem; noun: string; onClear: () => void }) {
  const entries = Object.entries(problem.errors);
  return (
    <div className="mb-4 rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert">
      <p className="font-semibold">Couldn't show {noun} with these filters. Nothing was changed.</p>
      {entries.length > 0 ? (
        <ul className="ml-5 mt-1 list-disc">{entries.map(([k, v]) => <li key={k}><code className="font-mono text-[13px]">{k}</code>: {v.join(" ")}</li>)}</ul>
      ) : <p className="mt-1">{problem.detail}</p>}
      <div className="mt-2"><Button size="sm" variant="outline" onClick={onClear}>Clear filters</Button></div>
    </div>
  );
}

function TableSkeleton({ what }: { what: string }) {
  return (
    <div aria-live="polite">
      <p className="sr-only">Loading {what}…</p>
      <div className="overflow-hidden rounded-lg border bg-card" aria-hidden="true">
        {Array.from({ length: 8 }, (_, i) => (
          <div key={i} className="flex items-center gap-4 border-b border-border-subtle px-4 py-4 last:border-0">
            <span className="h-3.5 w-1/4 rounded-sm bg-muted" /><span className="h-3.5 w-16 rounded-sm bg-muted" /><span className="h-3.5 w-1/3 rounded-sm bg-muted" />
          </div>
        ))}
      </div>
    </div>
  );
}

/** Comfortable (60 px rows) or compact (44 px rows), per user. Two pressed-state buttons. */
function DensityToggle({ density, onChange }: { density: Density; onChange: (d: Density) => void }) {
  return (
    <div role="group" aria-label="Row density" className="inline-flex overflow-hidden rounded-md border border-border-strong bg-card">
      {([["comfortable", "Comfortable", Rows3], ["compact", "Compact", Rows4]] as const).map(([value, label, Icon]) => (
        <button key={value} type="button" aria-pressed={density === value} onClick={() => onChange(value)}
          className="flex h-8 items-center gap-1.5 px-2.5 text-[13px] text-muted-foreground aria-pressed:bg-accent aria-pressed:font-semibold aria-pressed:text-accent-foreground [&+&]:border-l [&+&]:border-border-strong">
          <Icon aria-hidden="true" className="size-3.5" />{label}
        </button>
      ))}
    </div>
  );
}

/** Column choice: a menu button whose items are checkable (menuitemcheckbox). The menu stays open while columns are
 *  toggled; Esc, Tab and outside clicks close it and focus returns to the button. */
function ColumnPicker<T extends Row>({ table, columns, rowHeader, visibility }: {
  table: RecordTableState<T>; columns: RecordColumn<T>[]; rowHeader: string; visibility: Record<string, boolean>;
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  const choices = columns.filter((c) => c.id !== rowHeader);
  const isOn = (c: RecordColumn<T>) => visibility[c.sortField ?? c.id] !== false;
  const shown = choices.filter(isOn).length;
  useEffect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => { if (!wrap.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", away);
    return () => document.removeEventListener("pointerdown", away);
  }, [open]);
  const focusItem = (i: number) => items.current[(i + choices.length + 1) % (choices.length + 1)]?.focus();
  function toggle(c: RecordColumn<T>) {
    const off = choices.filter((x) => (x.id === c.id ? isOn(x) : !isOn(x))).map((x) => x.id);
    table.setHidden(off, columns.map((x) => x.id));
  }
  function onMenuKey(e: React.KeyboardEvent) {
    const at = items.current.findIndex((el) => el === document.activeElement);
    const moves: Record<string, () => void> = {
      ArrowDown: () => focusItem(at + 1), ArrowUp: () => focusItem(at - 1), Home: () => focusItem(0), End: () => focusItem(-1),
      Escape: () => { setOpen(false); button.current?.focus(); }, Tab: () => setOpen(false),
    };
    if (!moves[e.key]) return;
    if (e.key !== "Tab") e.preventDefault();
    moves[e.key]();
  }
  return (
    <div ref={wrap} className="relative">
      <button ref={button} type="button" className={buttonVariants({ variant: "outline", size: "sm" })} aria-haspopup="menu" aria-expanded={open}
        aria-controls={open ? `${table.id}-columns` : undefined}
        onClick={() => { setOpen(!open); if (!open) setTimeout(() => focusItem(0)); }}
        onKeyDown={(e) => { if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setTimeout(() => focusItem(0)); } }}>
        <Columns3 aria-hidden="true" className="text-icon" />Columns<span className="sr-only">, {shown + 1} of {choices.length + 1} shown</span><span aria-hidden="true">▾</span>
      </button>
      {open && (
        <div id={`${table.id}-columns`} role="menu" aria-label="Show columns" onKeyDown={onMenuKey}
          className="absolute right-0 z-40 mt-1 min-w-56 rounded-md border bg-popover p-1 text-popover-foreground shadow-md">
          {choices.map((c, i) => (
            <button key={c.id} ref={(el) => { items.current[i] = el; }} type="button" role="menuitemcheckbox" aria-checked={isOn(c)} tabIndex={-1}
              onClick={() => toggle(c)}
              className="flex w-full items-center rounded-sm px-3 py-2 text-left text-sm hover:bg-accent hover:text-accent-foreground focus-visible:bg-accent focus-visible:text-accent-foreground focus-visible:outline-none">
              <span aria-hidden="true" className="mr-2 inline-block w-3">{isOn(c) ? "✓" : ""}</span>{c.header}
            </button>
          ))}
          <div className="my-1 border-t border-border-subtle" />
          <button ref={(el) => { items.current[choices.length] = el; }} type="button" role="menuitem" tabIndex={-1}
            onClick={() => { table.setHidden(null); setOpen(false); button.current?.focus(); }}
            className="flex w-full items-center rounded-sm px-3 py-2 text-left text-sm hover:bg-accent hover:text-accent-foreground focus-visible:bg-accent focus-visible:text-accent-foreground focus-visible:outline-none">
            <span aria-hidden="true" className="mr-2 inline-block w-3" />Reset to default columns
          </button>
        </div>
      )}
    </div>
  );
}

/** The bulk action bar: replaces the page header's actions while rows are selected. Sticky, so it stays in reach on
 *  a long page. "Clear selection" returns focus to the select-all box. */
export function BulkBar<T extends Row>({ table, noun, children }: { table: RecordTableState<T>; noun: [string, string]; children: React.ReactNode }) {
  const n = table.selected.size;
  function clear() {
    table.clearSelection();
    setTimeout(() => document.querySelector<HTMLInputElement>('input[aria-label="Select all on this page"]')?.focus());
  }
  return (
    <div role="region" aria-label="Bulk actions"
      className="sticky top-0 z-30 mb-3 flex flex-wrap items-center gap-3 rounded-md border border-border-strong bg-accent px-4 py-2 text-accent-foreground">
      <p className="font-semibold">{n} selected<span className="sr-only"> {n === 1 ? noun[0] : noun[1]}</span>{n >= table.maxSelection ? ` (the most you can edit at once is ${table.maxSelection})` : ""}</p>
      {children}
      <Button size="sm" variant="outline" onClick={clear}>Clear selection</Button>
    </div>
  );
}
