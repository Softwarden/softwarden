/** Catalog reference data, loaded on first use (React.lazy in App.tsx):
 *
 *  - AI models (`/ai-models`), AI providers (`/ai-providers`) and the Models in use report (`/ai-models/in-use`), three
 *    pages under one "AI models" sidebar entry with a section switch. Filters in the URL.
 *  - Software categories (`/categories`) and Tags (`/tags`, colour and description).
 *
 *  Everyone reads; Editors and Admins write. A delete the API refuses because something uses the item (409) is shown in
 *  the dialog with what uses it and a link to those records; a row already known to be in use explains that instead of
 *  offering a delete that would fail. A model's and a category's usage counts archived records too, so their links open
 *  the list with archived records included. */
import { useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import {
  AIModel, AIProvider, api, isInUse, labelOf, MODEL_MODALITIES, ModelInUse, Problem, SoftwareCategory, TAG_COLORS, type TagInfo,
} from "@/api";
import { attempt, EmptyState, ErrorBanner, ErrorSummary, Field, Loading, PageHead, Select, TableRegion, Textarea } from "@/components/app";
import { FilterBar } from "@/components/filter-bar";
import { SuccessNote, TagChip } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Modal } from "@/features/admin/admin";
import { modelName } from "@/features/catalog/catalog-list";
import { OpenedEdit, type OpenedWrite } from "@/components/opened-edit";
import { ifMatch, isStaleWrite, staleDeleteText, useOpenedRecord } from "@/lib/if-match";
import { problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { canWrite, useMe } from "@/lib/session";

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

// ---- shared pieces -----------------------------------------------------------------------------------------------------

/** The AI models section switch: Models, Providers, Models in use (links, the current one marked). */
function AISections() {
  const { pathname } = useLocation();
  const sections = [
    { to: "/ai-models", label: "Models" },
    { to: "/ai-providers", label: "Providers" },
    { to: "/ai-models/in-use", label: "Models in use" },
  ];
  return (
    <nav aria-label="AI models sections" className="mb-4 inline-flex overflow-hidden rounded-md border border-border-strong bg-card">
      {sections.map((s) => (
        <Link key={s.to} to={s.to} aria-current={pathname === s.to ? "page" : undefined}
          className="flex h-8 items-center px-3 text-[13px] text-muted-foreground no-underline aria-[current=page]:bg-accent aria-[current=page]:font-semibold aria-[current=page]:text-accent-foreground [&+&]:border-l [&+&]:border-border-strong">
          {s.label}
        </Link>
      ))}
    </nav>
  );
}

/** Delete confirmation. In use already: says so, with a link to what uses it, and offers no delete. Otherwise asks; a
 *  409 the API sends (something started using it meanwhile) is shown here with the API's own words. */
function DeleteDialog({ open, onClose, name, inUse, usedBy, path, onDeleted }: {
  open: boolean; onClose: () => void; name: string; inUse: string | null; usedBy: React.ReactNode; path: string; onDeleted: () => void;
}) {
  const [refusal, setRefusal] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const close = () => { setRefusal(""); setError(""); onClose(); };
  // Read with its ETag as the confirmation opens; the delete sends it as If-Match.
  const opened = useOpenedRecord(open && path ? path : null);
  async function remove() {
    setBusy(true);
    setError("");
    try {
      await api(path, { method: "DELETE", headers: ifMatch(await opened.etag()) });
      setRefusal("");
      onDeleted();
    } catch (e) {
      if (isStaleWrite(e)) setError(staleDeleteText("item"));
      else if (isInUse(e)) setRefusal(e.detail);
      else setError(problemText(e, "Couldn't delete it. Try again."));
    } finally {
      setBusy(false);
    }
  }
  const blocked = refusal || inUse;
  return (
    <Modal title={`Delete ${name}?`} open={open} onClose={close}
      description={blocked ? `${name} is in use, so it can't be deleted.` : "Nothing uses it. Deleting it can't be undone; the audit log keeps a record."}>
      {blocked && (
        <div className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-state-pending-foreground" role="alert">
          <p><span aria-hidden="true">⚠ </span>{refusal || inUse}</p>
          <p className="mt-1">{usedBy}</p>
        </div>
      )}
      {error && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{error}</p>}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={close}>Cancel</Button>
        {!blocked && <Button type="button" variant="destructive" disabled={busy} onClick={() => void remove()}>{busy ? "Deleting…" : `Delete ${name}`}</Button>}
      </DialogFooter>
    </Modal>
  );
}

function ViewerNote({ what }: { what: string }) {
  return <p className="mb-3 text-sm text-muted-foreground">You can view {what}. Editors and Admins can add, change and delete them.</p>;
}

// ---- AI models -----------------------------------------------------------------------------------------------------------

const IN_USE_OPTIONS = [{ value: "true", label: "Used by an agent" }, { value: "false", label: "Not used" }];

export function AIModelsPage() {
  const me = useMe();
  const write = canWrite(me);
  const [params, setParams] = useSearchParams();
  const values = { provider: params.get("provider") ?? "", modality: params.get("modality") ?? "", in_use: params.get("in_use") ?? "" };
  const q = params.get("q") ?? "";
  const query = new URLSearchParams(Object.entries({ ...values, q }).filter(([, v]) => v)).toString();
  const models = useApiQuery<AIModel[]>(`ai-models${query ? `?${query}` : ""}`);
  const providers = useApiQuery<AIProvider[]>("ai-providers");
  const invalidate = useInvalidate();
  const [editing, setEditing] = useState<AIModel | "new" | null>(null);
  const [deleting, setDeleting] = useState<AIModel | null>(null);
  const [notice, setNotice] = useState("");
  // The search and typed chips replace the history entry; a discrete filter change pushes one.
  const set = (k: string, v: string, typed = false) => setParams((cur) => { const n = new URLSearchParams(cur); if (v) n.set(k, v); else n.delete(k); return n; }, { replace: k === "q" || typed });
  const filtered = !!query;
  const clear = () => setParams({});
  const done = (text: string) => { setEditing(null); setDeleting(null); setNotice(text); void invalidate("ai-models", "ai-providers"); };
  const rows = models.data;
  const noProviders = providers.data?.length === 0;
  return (
    <>
      <PageHead title="AI models" desc="The models your agents use, by provider. Agents pick from this list.">
        {write && <Button onClick={() => setEditing("new")} disabled={noProviders}>Add model</Button>}
      </PageHead>
      <AISections />
      {!write && <ViewerNote what="AI models" />}
      {write && noProviders && <p className="mb-3 text-sm text-muted-foreground">Add a provider first: every model belongs to one. <Link to="/ai-providers">Go to AI providers</Link>.</p>}
      <FilterBar id="models" searchLabel="Search AI models" searchPlaceholder="Name or version" search={q} onSearch={(v) => set("q", v)}
        fields={[
          { key: "provider", label: "Provider", options: (providers.data ?? []).map((p) => ({ value: p.id, label: p.name })) },
          { key: "modality", label: "Modality", options: MODEL_MODALITIES },
          { key: "in_use", label: "Use", options: IN_USE_OPTIONS },
        ]}
        values={values} onChange={set} filtered={filtered} onClear={clear} />
      <SuccessNote text={notice} />
      {models.isError && <ErrorBanner message={`${problemText(models.error, "Couldn't load AI models.")} Nothing was changed.`} onRetry={() => void models.refetch()} />}
      {models.isPending && <Loading what="AI models" rows={4} />}
      {rows && rows.length === 0 && (filtered ? (
        <EmptyState title="No AI models match these filters" action={<Button variant="outline" onClick={clear}>Clear filters</Button>}>Try another provider or modality, or clear the filters.</EmptyState>
      ) : (
        <EmptyState title="No AI models yet" action={write && !noProviders ? <Button variant="outline" onClick={() => setEditing("new")}>Add model</Button> : undefined}>
          {write ? "Record each model your agents use, such as GPT-4o or Claude Sonnet, with its provider and version." : "Editors and Admins record the models agents use here."}
        </EmptyState>
      ))}
      {rows && rows.length > 0 && (
        <TableRegion label="AI models table">
          <Table>
            <TableCaption className="sr-only">AI models, by name and version</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Model</TableHead><TableHead scope="col">Version</TableHead><TableHead scope="col">Provider</TableHead>
              <TableHead scope="col">Modality</TableHead><TableHead scope="col">Hosting region</TableHead><TableHead scope="col">Used by</TableHead>
              {write && <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>}
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((m) => (
                <TableRow key={m.id}>
                  <TableHead scope="row" className="font-semibold text-foreground">{m.name}</TableHead>
                  <TableCell>{m.version || "—"}</TableCell>
                  <TableCell>{m.provider.name}</TableCell>
                  <TableCell>{labelOf(MODEL_MODALITIES, m.modality) || "—"}</TableCell>
                  <TableCell>{m.hosting_region || "—"}</TableCell>
                  <TableCell>{m.usage ? <Link to={`/ai-agents?model=${m.id}&archived=all`}>{plural(m.usage, "agent", "agents")}<span className="sr-only"> use {modelName(m)}</span></Link> : "No agents"}</TableCell>
                  {write && (
                    <TableCell className="space-x-2 text-right whitespace-nowrap">
                      <Button size="sm" variant="outline" onClick={() => setEditing(m)}>Edit<span className="sr-only"> {modelName(m)}</span></Button>
                      <Button size="sm" variant="outline" onClick={() => setDeleting(m)}>Delete<span className="sr-only"> {modelName(m)}</span></Button>
                    </TableCell>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      <Modal title={editing === "new" ? "Add AI model" : `Edit ${editing ? modelName(editing) : ""}`} open={editing !== null} onClose={() => setEditing(null)}
        description="Name and version are unique per provider.">
        {editing === "new" && <ModelForm model={null} providers={providers.data ?? []} onCancel={() => setEditing(null)} onDone={done} />}
        {editing && editing !== "new" && (
          <OpenedEdit<AIModel> path={`ai-models/${editing.id}`} what="AI model">
            {(model, write) => <ModelForm model={model} write={write} providers={providers.data ?? []} onCancel={() => setEditing(null)} onDone={done} />}
          </OpenedEdit>
        )}
      </Modal>
      <DeleteDialog open={deleting !== null} onClose={() => setDeleting(null)} name={deleting ? modelName(deleting) : ""} path={`ai-models/${deleting?.id}`}
        inUse={deleting && deleting.usage ? `${plural(deleting.usage, "agent uses", "agents use")} it, archived agents included. Remove it from them first.` : null}
        usedBy={deleting && <Link to={`/ai-agents?model=${deleting.id}&archived=all`}>Show the agents that use {modelName(deleting)}</Link>}
        onDeleted={() => done(`${deleting ? modelName(deleting) : "The model"} deleted.`)} />
    </>
  );
}

function ModelForm({ model, write, providers, onCancel, onDone }: { model: AIModel | null; write?: OpenedWrite; providers: AIProvider[]; onCancel: () => void; onDone: (t: string) => void }) {
  const [form, setForm] = useState({
    provider: model?.provider.id ?? (providers.length === 1 ? providers[0].id : ""), name: model?.name ?? "", version: model?.version ?? "",
    modality: model?.modality ?? "", hosting_region: model?.hosting_region ?? "",
  });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  const on = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const found: Record<string, string[]> = {};
    if (!form.provider) found.provider = ["Choose a provider."];
    if (!form.name.trim()) found.name = ["Enter a name."];
    if (Object.keys(found).length) return setErrors(found);
    setBusy(true);
    const body = { ...form, name: form.name.trim(), version: form.version.trim(), hosting_region: form.hosting_region.trim() };
    const failed = await attempt(async () => api(model ? `ai-models/${model.id}` : "ai-models", { method: model ? "PATCH" : "POST", body, headers: await write?.ifMatch() }));
    setBusy(false);
    if (failed?.status === 412 && write) return write.onStale();
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { name: [failed.detail] });
    const label = `${providers.find((p) => p.id === form.provider)?.name ?? ""} ${body.name}${body.version ? ` ${body.version}` : ""}`.trim();
    onDone(model ? `${label} saved.` : `${label} added.`);
  }
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      <Field id="provider" label="Provider (required)" errors={errors}>
        <Select id="provider" value={form.provider} onChange={on("provider")}>
          <option value="">Choose a provider</option>
          {providers.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </Select>
      </Field>
      <Field id="name" label="Name (required)" errors={errors} help="For example GPT-4o or Claude Sonnet."><Input id="name" value={form.name} onChange={on("name")} autoComplete="off" /></Field>
      <Field id="version" label="Version" errors={errors} help="For example 2024-08-06 or 4.5."><Input id="version" value={form.version} onChange={on("version")} autoComplete="off" /></Field>
      <Field id="modality" label="Modality" errors={errors}>
        <Select id="modality" value={form.modality} onChange={on("modality")}>
          <option value="">Not recorded</option>
          {MODEL_MODALITIES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
        </Select>
      </Field>
      <Field id="hosting_region" label="Hosting region" errors={errors} help="For example Sweden Central or EU."><Input id="hosting_region" value={form.hosting_region} onChange={on("hosting_region")} autoComplete="off" /></Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={busy}>{busy ? "Saving…" : model ? "Save" : "Add model"}</Button>
      </DialogFooter>
    </form>
  );
}

// ---- AI providers --------------------------------------------------------------------------------------------------------

export function AIProvidersPage() {
  const me = useMe();
  const write = canWrite(me);
  const providers = useApiQuery<AIProvider[]>("ai-providers");
  const invalidate = useInvalidate();
  const [editing, setEditing] = useState<AIProvider | "new" | null>(null);
  const [deleting, setDeleting] = useState<AIProvider | null>(null);
  const [notice, setNotice] = useState("");
  const done = (text: string) => { setEditing(null); setDeleting(null); setNotice(text); void invalidate("ai-providers", "ai-models"); };
  const rows = providers.data;
  return (
    <>
      <PageHead title="AI providers" desc="Who makes or hosts the models your agents use.">
        {write && <Button onClick={() => setEditing("new")}>Add provider</Button>}
      </PageHead>
      <AISections />
      {!write && <ViewerNote what="AI providers" />}
      <SuccessNote text={notice} />
      {providers.isError && <ErrorBanner message={`${problemText(providers.error, "Couldn't load AI providers.")} Nothing was changed.`} onRetry={() => void providers.refetch()} />}
      {providers.isPending && <Loading what="AI providers" rows={3} />}
      {rows && rows.length === 0 && (
        <EmptyState title="No AI providers yet" action={write ? <Button variant="outline" onClick={() => setEditing("new")}>Add provider</Button> : undefined}>
          {write ? "Add a provider, such as OpenAI, Anthropic or Microsoft, then its models." : "Editors and Admins record AI providers here."}
        </EmptyState>
      )}
      {rows && rows.length > 0 && (
        <TableRegion label="AI providers table">
          <Table>
            <TableCaption className="sr-only">AI providers, by name</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Provider</TableHead><TableHead scope="col">Website</TableHead><TableHead scope="col">Models</TableHead>
              {write && <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>}
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((p) => (
                <TableRow key={p.id}>
                  <TableHead scope="row" className="font-semibold text-foreground">{p.name}</TableHead>
                  <TableCell className="[overflow-wrap:anywhere]">{p.website ? <a href={p.website} target="_blank" rel="noreferrer noopener">{p.website}<span className="sr-only"> (opens in a new tab)</span></a> : "—"}</TableCell>
                  <TableCell>{p.usage ? <Link to={`/ai-models?provider=${p.id}`}>{plural(p.usage, "model", "models")}<span className="sr-only"> from {p.name}</span></Link> : "No models"}</TableCell>
                  {write && (
                    <TableCell className="space-x-2 text-right whitespace-nowrap">
                      <Button size="sm" variant="outline" onClick={() => setEditing(p)}>Edit<span className="sr-only"> {p.name}</span></Button>
                      <Button size="sm" variant="outline" onClick={() => setDeleting(p)}>Delete<span className="sr-only"> {p.name}</span></Button>
                    </TableCell>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      <Modal title={editing === "new" ? "Add AI provider" : `Edit ${editing ? editing.name : ""}`} open={editing !== null} onClose={() => setEditing(null)}>
        {editing === "new" && <ProviderForm provider={null} onCancel={() => setEditing(null)} onDone={done} />}
        {editing && editing !== "new" && (
          <OpenedEdit<AIProvider> path={`ai-providers/${editing.id}`} what="AI provider">
            {(provider, write) => <ProviderForm provider={provider} write={write} onCancel={() => setEditing(null)} onDone={done} />}
          </OpenedEdit>
        )}
      </Modal>
      <DeleteDialog open={deleting !== null} onClose={() => setDeleting(null)} name={deleting?.name ?? ""} path={`ai-providers/${deleting?.id}`}
        inUse={deleting && deleting.usage ? `${plural(deleting.usage, "model belongs", "models belong")} to it. Delete or move its models first.` : null}
        usedBy={deleting && <Link to={`/ai-models?provider=${deleting.id}`}>Show the models from {deleting.name}</Link>}
        onDeleted={() => done(`${deleting?.name ?? "The provider"} deleted.`)} />
    </>
  );
}

function ProviderForm({ provider, write, onCancel, onDone }: { provider: AIProvider | null; write?: OpenedWrite; onCancel: () => void; onDone: (t: string) => void }) {
  const [form, setForm] = useState({ name: provider?.name ?? "", website: provider?.website ?? "" });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.name.trim()) return setErrors({ name: ["Enter a name."] });
    setBusy(true);
    const body = { name: form.name.trim(), website: form.website.trim() };
    const failed = await attempt(async () => api(provider ? `ai-providers/${provider.id}` : "ai-providers", { method: provider ? "PATCH" : "POST", body, headers: await write?.ifMatch() }));
    setBusy(false);
    if (failed?.status === 412 && write) return write.onStale();
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { name: [failed.detail] });
    onDone(provider ? `${body.name} saved.` : `${body.name} added.`);
  }
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      <Field id="name" label="Name (required)" errors={errors}><Input id="name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} autoComplete="off" /></Field>
      <Field id="website" label="Website" errors={errors} help="Optional, for example https://openai.example"><Input id="website" type="url" value={form.website} onChange={(e) => setForm({ ...form, website: e.target.value })} /></Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={busy}>{busy ? "Saving…" : provider ? "Save" : "Add provider"}</Button>
      </DialogFooter>
    </form>
  );
}

// ---- Models in use ---------------------------------------------------------------------------------------------------------

export function ModelsInUsePage() {
  const report = useApiQuery<ModelInUse[]>("ai-models/in-use");
  const rows = report.data;
  return (
    <>
      <PageHead title="Models in use" desc="Each model that at least one unarchived agent uses, most used first. Live agents are Active or Deprecated." />
      <AISections />
      {report.isError && <ErrorBanner message={problemText(report.error, "Couldn't load the report.")} onRetry={() => void report.refetch()} />}
      {report.isPending && <Loading what="the models in use" rows={3} />}
      {rows && rows.length === 0 && (
        <EmptyState title="No agent uses a model yet" action={<Button asChild variant="outline"><Link to="/ai-agents">Go to AI agents</Link></Button>}>
          Models appear here once an agent records the models it uses. Archived agents don't count.
        </EmptyState>
      )}
      {rows && rows.length > 0 && (
        <TableRegion label="Models in use table">
          <Table>
            <TableCaption className="sr-only">Models in use, most used first, then by name</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Model</TableHead><TableHead scope="col">Provider</TableHead><TableHead scope="col">Modality</TableHead>
              <TableHead scope="col">Hosting region</TableHead><TableHead scope="col" aria-sort="descending">Agents</TableHead><TableHead scope="col">Live agents</TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((m) => (
                <TableRow key={m.id}>
                  <TableHead scope="row" className="font-semibold text-foreground">{m.name}{m.version && <span className="font-normal text-muted-foreground"> {m.version}</span>}</TableHead>
                  <TableCell>{m.provider.name}</TableCell>
                  <TableCell>{labelOf(MODEL_MODALITIES, m.modality) || "—"}</TableCell>
                  <TableCell>{m.hosting_region || "—"}</TableCell>
                  <TableCell className="tabular"><Link to={`/ai-agents?model=${m.id}`}>{m.agents}<span className="sr-only"> {m.agents === 1 ? "agent uses" : "agents use"} {modelName(m)}</span></Link></TableCell>
                  <TableCell className="tabular">{m.live_agents}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
    </>
  );
}

// ---- Software categories ----------------------------------------------------------------------------------------------------

export function CategoriesPage() {
  const me = useMe();
  const write = canWrite(me);
  const categories = useApiQuery<SoftwareCategory[]>("categories");
  const invalidate = useInvalidate();
  const [editing, setEditing] = useState<SoftwareCategory | "new" | null>(null);
  const [deleting, setDeleting] = useState<SoftwareCategory | null>(null);
  const [notice, setNotice] = useState("");
  const done = (text: string) => { setEditing(null); setDeleting(null); setNotice(text); void invalidate("categories"); };
  const rows = categories.data;
  return (
    <>
      <PageHead title="Software categories" desc="The categories applications are filed under. A new organization starts with a default list.">
        {write && <Button onClick={() => setEditing("new")}>Add category</Button>}
      </PageHead>
      {!write && <ViewerNote what="software categories" />}
      <SuccessNote text={notice} />
      {categories.isError && <ErrorBanner message={`${problemText(categories.error, "Couldn't load software categories.")} Nothing was changed.`} onRetry={() => void categories.refetch()} />}
      {categories.isPending && <Loading what="software categories" rows={4} />}
      {rows && rows.length === 0 && (
        <EmptyState title="No software categories" action={write ? <Button variant="outline" onClick={() => setEditing("new")}>Add category</Button> : undefined}>
          {write ? "Add categories such as Finance or Collaboration to group applications." : "Editors and Admins keep the list of categories."}
        </EmptyState>
      )}
      {rows && rows.length > 0 && (
        <TableRegion label="Software categories table">
          <Table>
            <TableCaption className="sr-only">Software categories, by name</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Category</TableHead><TableHead scope="col">Description</TableHead><TableHead scope="col">Applications</TableHead>
              {write && <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>}
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((c) => (
                <TableRow key={c.id}>
                  <TableHead scope="row" className="font-semibold text-foreground">{c.name}</TableHead>
                  <TableCell className="max-w-md whitespace-normal">{c.description || "—"}</TableCell>
                  <TableCell>{c.usage ? <Link to={`/applications?category=${c.id}&archived=all`}>{plural(c.usage, "application", "applications")}<span className="sr-only"> in {c.name}</span></Link> : "None"}</TableCell>
                  {write && (
                    <TableCell className="space-x-2 text-right whitespace-nowrap">
                      <Button size="sm" variant="outline" onClick={() => setEditing(c)}>Edit<span className="sr-only"> {c.name}</span></Button>
                      <Button size="sm" variant="outline" onClick={() => setDeleting(c)}>Delete<span className="sr-only"> {c.name}</span></Button>
                    </TableCell>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      <Modal title={editing === "new" ? "Add software category" : `Edit ${editing ? editing.name : ""}`} open={editing !== null} onClose={() => setEditing(null)}>
        {editing === "new" && <CategoryForm category={null} onCancel={() => setEditing(null)} onDone={done} />}
        {editing && editing !== "new" && (
          <OpenedEdit<SoftwareCategory> path={`categories/${editing.id}`} what="category">
            {(category, write) => <CategoryForm category={category} write={write} onCancel={() => setEditing(null)} onDone={done} />}
          </OpenedEdit>
        )}
      </Modal>
      <DeleteDialog open={deleting !== null} onClose={() => setDeleting(null)} name={deleting?.name ?? ""} path={`categories/${deleting?.id}`}
        inUse={deleting && deleting.usage ? `${plural(deleting.usage, "application is", "applications are")} in this category. Move them to another category first.` : null}
        usedBy={deleting && <Link to={`/applications?category=${deleting.id}&archived=all`}>Show the applications in {deleting.name}</Link>}
        onDeleted={() => done(`${deleting?.name ?? "The category"} deleted.`)} />
    </>
  );
}

function CategoryForm({ category, write, onCancel, onDone }: { category: SoftwareCategory | null; write?: OpenedWrite; onCancel: () => void; onDone: (t: string) => void }) {
  const [form, setForm] = useState({ name: category?.name ?? "", description: category?.description ?? "" });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.name.trim()) return setErrors({ name: ["Enter a name."] });
    setBusy(true);
    const body = { name: form.name.trim(), description: form.description.trim() };
    const failed = await attempt(async () => api(category ? `categories/${category.id}` : "categories", { method: category ? "PATCH" : "POST", body, headers: await write?.ifMatch() }));
    setBusy(false);
    if (failed?.status === 412 && write) return write.onStale();
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { name: [failed.detail] });
    onDone(category ? `${body.name} saved.` : `${body.name} added.`);
  }
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      <Field id="name" label="Name (required)" errors={errors} help="Unique in the organization, ignoring case."><Input id="name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} autoComplete="off" /></Field>
      <Field id="description" label="Description" errors={errors} help="Optional, up to 200 characters.">
        <Textarea id="description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={busy}>{busy ? "Saving…" : category ? "Save" : "Add category"}</Button>
      </DialogFooter>
    </form>
  );
}

// ---- Tags ------------------------------------------------------------------------------------------------------------------

const COLOR_NAME: Record<string, string> = Object.fromEntries(TAG_COLORS.map((c) => [c, c.charAt(0).toUpperCase() + c.slice(1)]));

export function TagsPage() {
  const me = useMe();
  const write = canWrite(me);
  const tags = useApiQuery<TagInfo[]>("tags");
  const invalidate = useInvalidate();
  const [editing, setEditing] = useState<TagInfo | null>(null);
  const [notice, setNotice] = useState("");
  const rows = tags.data;
  return (
    <>
      <PageHead title="Tags" desc="Labels on records. A tag is created the first time it's used on a record and leaves this list when nothing carries it." />
      {!write && <ViewerNote what="tags" />}
      <SuccessNote text={notice} />
      {tags.isError && <ErrorBanner message={`${problemText(tags.error, "Couldn't load tags.")} Nothing was changed.`} onRetry={() => void tags.refetch()} />}
      {tags.isPending && <Loading what="tags" rows={4} />}
      {rows && rows.length === 0 && (
        <EmptyState title="No tags yet" action={<Button asChild variant="outline"><Link to="/applications">Go to Applications</Link></Button>}>
          Add a tag on a record's edit form, for example Finance. It then appears here, where its color and description can be set.
        </EmptyState>
      )}
      {rows && rows.length > 0 && (
        <TableRegion label="Tags table">
          <Table>
            <TableCaption className="sr-only">Tags, by name, with their color and description</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Tag</TableHead><TableHead scope="col">Color</TableHead><TableHead scope="col">Description</TableHead><TableHead scope="col">Records</TableHead>
              {write && <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>}
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((t) => (
                <TableRow key={t.id}>
                  <TableHead scope="row" className="font-semibold text-foreground"><TagChip name={t.name} color={t.color} /></TableHead>
                  <TableCell>{t.color ? COLOR_NAME[t.color] ?? t.color : "Neutral (default)"}</TableCell>
                  <TableCell className="max-w-md whitespace-normal">{t.description || "—"}</TableCell>
                  <TableCell className="tabular">{t.count}</TableCell>
                  {write && <TableCell className="text-right"><Button size="sm" variant="outline" onClick={() => setEditing(t)}>Edit<span className="sr-only"> tag {t.name}</span></Button></TableCell>}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      <Modal title={`Edit tag ${editing?.name ?? ""}`} open={editing !== null} onClose={() => setEditing(null)}
        description="The color shows on the tag's chips everywhere; the name always shows too. Rename a tag by changing it on its records.">
        {editing && (
          <OpenedEdit<TagInfo> path={`tags/${editing.id}`} what="tag">
            {(tag, write) => <TagForm tag={tag} write={write} onCancel={() => setEditing(null)} onDone={(text) => { setEditing(null); setNotice(text); void invalidate("tags"); }} />}
          </OpenedEdit>
        )}
      </Modal>
    </>
  );
}

function TagForm({ tag, write, onCancel, onDone }: { tag: TagInfo; write: OpenedWrite; onCancel: () => void; onDone: (t: string) => void }) {
  const [color, setColor] = useState(tag.color ?? "");
  const [description, setDescription] = useState(tag.description ?? "");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api(`tags/${tag.id}`, { method: "PATCH", body: { color, description: description.trim() }, headers: await write.ifMatch() });
      onDone(`Tag ${tag.name} saved.`);
    } catch (err) {
      if (isStaleWrite(err)) write.onStale();
      else setErrors(err instanceof Problem && Object.keys(err.errors).length ? err.errors : { description: [problemText(err)] });
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      <fieldset className="mb-3">
        <legend className="mb-1.5 text-sm font-medium">Color</legend>
        <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
          {["", ...TAG_COLORS].map((c, i) => (
            <label key={c || "none"} className="flex min-h-8 cursor-pointer items-center gap-2 rounded-sm px-1 text-sm">
              <input type="radio" name="tag-color" id={i === 0 ? "color" : undefined} value={c} checked={color === c} onChange={() => setColor(c)} className="h-4 w-4 accent-primary" />
              <TagChip name={c ? COLOR_NAME[c] : "Neutral"} color={c || undefined} />
            </label>
          ))}
        </div>
        {errors.color && <span className="text-sm text-error-foreground">{errors.color.join(" ")}</span>}
      </fieldset>
      <p className="mb-3 text-sm">Preview: <TagChip name={tag.name} color={color || undefined} /></p>
      <Field id="description" label="Description" errors={errors} help="Optional. What the tag means, shown when pointing at a chip.">
        <Textarea id="description" value={description} onChange={(e) => setDescription(e.target.value)} />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={busy}>{busy ? "Saving…" : "Save tag"}</Button>
      </DialogFooter>
    </form>
  );
}
