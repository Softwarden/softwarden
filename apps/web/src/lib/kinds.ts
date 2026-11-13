/** Record kinds in one place: the API path of each kind, its page in the SPA, its label and icon.
 *
 *  Mirrors the API's `catalog.models.KIND_PATHS` (application → applications, component → components, ai_agent →
 *  ai-agents), plus integrations, which have their own pages. Every link to a record goes through `recordPath`, so a new
 *  kind can never fall through to another kind's page (before this helper the SPA built `/${kind}s/` and sent unknown
 *  kinds to `/components/<id>`). */
import { ArrowLeftRight, Bot, Cog, LayoutGrid, type LucideIcon } from "lucide-react";

/** The Resource kinds with ownership, reviews and the shared record fields. */
export type RecordKind = "application" | "component" | "ai_agent";
/** Every kind with its own record page. */
export type CatalogKind = RecordKind | "integration";

export const RECORD_KINDS: RecordKind[] = ["application", "component", "ai_agent"];

const API_PATHS: Record<CatalogKind, string> = {
  application: "applications",
  component: "components",
  ai_agent: "ai-agents",
  integration: "integrations",
};

export const KIND_LABEL: Record<CatalogKind, string> = {
  application: "Application",
  component: "Component",
  ai_agent: "AI agent",
  integration: "Integration",
};

/** For headings and list names: "AI agents". */
export const KIND_PLURAL: Record<CatalogKind, string> = {
  application: "Applications",
  component: "Components",
  ai_agent: "AI agents",
  integration: "Integrations",
};

/** For sentences: ["AI agent", "AI agents"] ("3 AI agents"); the acronym keeps its capitals. */
export const KIND_NOUN: Record<CatalogKind, [string, string]> = {
  application: ["application", "applications"],
  component: ["component", "components"],
  ai_agent: ["AI agent", "AI agents"],
  integration: ["integration", "integrations"],
};

export const KIND_ICON: Record<CatalogKind, LucideIcon> = {
  application: LayoutGrid,
  component: Cog,
  ai_agent: Bot,
  integration: ArrowLeftRight,
};

export function isCatalogKind(kind: unknown): kind is CatalogKind {
  return typeof kind === "string" && kind in API_PATHS;
}

export function isRecordKind(kind: unknown): kind is RecordKind {
  return kind === "application" || kind === "component" || kind === "ai_agent";
}

/** The API collection for a kind, relative to /api/v1/: `apiPath("ai_agent")` is "ai-agents". */
export function apiPath(kind: CatalogKind): string {
  return API_PATHS[kind];
}

/** The SPA page of one record, optionally on a tab: `recordPath("ai_agent", id, "relationships")`. Kinds outside
 *  CatalogKind (a value a newer API sends) have no page: null, so the caller shows the name without a link. */
export function recordPath(kind: string, id: string, tab?: string): string | null {
  if (!isCatalogKind(kind)) return null;
  return `/${API_PATHS[kind]}/${id}${tab ? `?tab=${tab}` : ""}`;
}

/** `recordPath` for a reference with `kind` and `id` (rows, link ends, audit targets). */
export const pathOf = (r: { kind: string; id: string }, tab?: string) => recordPath(r.kind, r.id, tab);

export function editPath(kind: RecordKind | "integration", id: string): string {
  return `/${API_PATHS[kind]}/${id}/edit`;
}

/** The list a kind belongs to. Components share the Applications page (a Kind switch). */
export function listPath(kind: CatalogKind): string {
  return kind === "component" ? "/applications?kind=components" : `/${API_PATHS[kind]}`;
}

export function newPath(kind: CatalogKind): string {
  return kind === "component" ? "/applications/new?kind=component" : `/${API_PATHS[kind]}/new`;
}
