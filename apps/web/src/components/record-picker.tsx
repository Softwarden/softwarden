/** Searchable pick of a catalog record (application, component or integration) through the list APIs' `q` search,
 *  and the optional instance pick that follows it. Used by the relationship dialogs and the integration form. */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, cap, LinkKind, Page } from "@/api";
import { Select, useLoad } from "@/components/app";
import { Combobox, ComboOption } from "@/components/combobox";
import { apiPath, KIND_LABEL } from "@/lib/kinds";

export type PickedRecord = { id: string; name: string; kind: LinkKind; lifecycle?: string };
type Found = { id: string; name: string; lifecycle: string };

export const kindLabel = (k: LinkKind) => KIND_LABEL[k] ?? cap(k);

export function RecordPicker({ id, value, onChange, kinds, placeholder, listLabel, disabled, ...aria }: {
  id: string; value: PickedRecord | null; onChange: (r: PickedRecord | null) => void; kinds: LinkKind[];
  placeholder?: string; listLabel?: string; disabled?: boolean; "aria-invalid"?: boolean; "aria-describedby"?: string;
}) {
  const [text, setText] = useState("");
  const [found, setFound] = useState<PickedRecord[]>([]);
  const [busy, setBusy] = useState(true);
  const [failed, setFailed] = useState(false);
  const request = useRef(0);
  const kindKey = kinds.join(",");

  const search = useCallback(() => {
    const mine = ++request.current;
    setBusy(true);
    setFailed(false);
    const q = text ? `&q=${encodeURIComponent(text)}` : "";
    Promise.all(kindKey.split(",").map((k) => api<Page<Found>>(`${apiPath(k as LinkKind)}?page_size=8${q}`).then((p) => p.results.map((r): PickedRecord => ({ ...r, kind: k as LinkKind })))))
      .then((lists) => { if (mine === request.current) setFound(lists.flat()); })
      .catch(() => { if (mine === request.current) { setFound([]); setFailed(true); } })
      .finally(() => { if (mine === request.current) setBusy(false); });
  }, [text, kindKey]);
  // The first search runs at once; typing waits for a short pause.
  const first = useRef(true);
  useEffect(() => {
    if (first.current) { first.current = false; search(); return; }
    const t = setTimeout(search, 250);
    return () => clearTimeout(t);
  }, [search]);

  const records = value && !found.some((r) => r.id === value.id) ? [value, ...found] : found;
  const options: ComboOption[] = records.map((r) => ({ id: r.id, label: r.name, hint: `${kindLabel(r.kind)}${r.lifecycle ? ` · ${cap(r.lifecycle)}` : ""}` }));
  return (
    <>
      <Combobox id={id} options={options} value={value?.id ?? ""} disabled={disabled} placeholder={placeholder ?? "Search by name…"}
        listLabel={listLabel ?? "Record suggestions"} onChange={(v) => onChange(records.find((r) => r.id === v) ?? null)}
        remote={{ onQueryChange: setText, busy }} {...aria} />
      {failed && <p className="mt-1 text-sm text-error-foreground" role="alert">Couldn't search records. <button type="button" className="underline" onClick={search}>Retry</button></p>}
    </>
  );
}

type Instance = { id: string; name: string; environment?: string };

/** Tracks instance pickers whose lookup failed and that the editor hasn't settled yet. Pass
 *  `report` to each InstancePicker as `onBlocked`, and check `errors()` before saving: an unset instance means "the
 *  whole application", so a failed lookup must not look like an application without deployments. */
export function useInstanceGate() {
  const [blocked, setBlocked] = useState<Record<string, boolean>>({});
  const report = useCallback((id: string, b: boolean) => setBlocked((cur) => (Boolean(cur[id]) === b ? cur : { ...cur, [id]: b })), []);
  const errors = (): Record<string, string[]> => Object.fromEntries(
    Object.entries(blocked).filter(([, b]) => b).map(([id]) => [id, ["The deployments couldn't be loaded. Retry, or choose to link the application as a whole."]]),
  );
  return { report, errors };
}

/** Instance pick for an application end. Renders nothing for components, integrations or applications without
 *  instances. If the lookup fails it says so, with Retry and an explicit "the application as a whole" choice. */
export function InstancePicker(props: {
  id: string; label: string; record: PickedRecord | null; value: string; onChange: (id: string) => void; errors?: Record<string, string[]>;
  onBlocked?: (id: string, blocked: boolean) => void;
}) {
  return props.record?.kind === "application" ? <InstanceSelect key={props.record.id} app={props.record.id} appName={props.record.name} {...props} /> : null;
}

function InstanceSelect({ app, appName, id, label, value, onChange, errors, onBlocked }: {
  app: string; appName: string; id: string; label: string; value: string; onChange: (id: string) => void; errors?: Record<string, string[]>;
  onBlocked?: (id: string, blocked: boolean) => void;
}) {
  const load = useLoad<Instance[]>(`applications/${app}/instances`);
  const [whole, setWhole] = useState(false);
  const failed = Boolean(load.error);
  useEffect(() => { onBlocked?.(id, failed && !whole); }, [id, failed, whole, onBlocked]);
  useEffect(() => () => onBlocked?.(id, false), [id, onBlocked]);
  const err = errors?.[id];
  if (failed) {
    return (
      <fieldset className="mb-3 flex flex-col gap-1.5" aria-describedby={err ? `${id}-error` : undefined}>
        <legend className="mb-1.5 text-sm font-medium leading-none">{label}</legend>
        <p className="text-sm text-error-foreground" role="alert">Couldn't load the deployments of {appName}. {load.error}</p>
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" className="underline" onClick={() => { setWhole(false); load.reload(); }}>Retry</button>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" id={id} checked={whole} onChange={(e) => setWhole(e.target.checked)} className="h-4 w-4 accent-primary" />
            Link {appName} as a whole (no particular deployment)
          </label>
        </div>
        {err && <span className="text-sm text-error-foreground" id={`${id}-error`}>{err.join(" ")}</span>}
      </fieldset>
    );
  }
  if (!load.data || load.data.length === 0) return null;
  return (
    <div className="mb-3 flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium leading-none">{label}</label>
      <Select id={id} value={value} onChange={(e) => onChange(e.target.value)} aria-invalid={err ? true : undefined} aria-describedby={err ? `${id}-error` : `${id}-help`}>
        <option value="">Not specified (the application as a whole)</option>
        {load.data.map((i) => <option key={i.id} value={i.id}>{i.name}{i.environment ? ` (${cap(i.environment)})` : ""}</option>)}
      </Select>
      <span className="text-sm text-muted-foreground" id={`${id}-help`}>Optional. Pick one when only this deployment is involved.</span>
      {err && <span className="text-sm text-error-foreground" id={`${id}-error`}>{err.join(" ")}</span>}
    </div>
  );
}
