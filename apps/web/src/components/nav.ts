/** The app's pages, in one list shared by the grouped sidebar, the command palette's Pages group and the `g`
 *  shortcuts. A page appears only when it exists and the role may open it; no empty links. */
import { Activity, ArrowLeftRight, CalendarX2, Waypoints, EyeOff, UserX, FileCode, BarChart3, BellRing, Bot, BrainCircuit, Building2, CalendarClock, ClipboardCheck, Cog, FileBarChart, FileInput, FileSignature, Gauge, FolderTree, History, Inbox, KeyRound, LayoutGrid, Link2, ListChecks, ListPlus, Plug, Network, Radar, Scale, ScrollText, Send, Server, Settings, ShieldAlert, ShieldCheck, Lightbulb, Tags, TicketCheck, Ticket, Upload, UserCheck, UserCog, Users, Webhook, Logs, BookOpen, type LucideIcon } from "lucide-react";
import type { Me } from "@/api";

export type NavSection = "Work" | "Catalog" | "Money" | "Discovery" | "Admin";
export const NAV_SECTIONS: NavSection[] = ["Work", "Catalog", "Money", "Discovery", "Admin"];

export type NavItem = {
  id: string;
  label: string;
  to: string;
  icon: LucideIcon;
  section: NavSection;
  /** The second key after `g` ("a" for `g a`). */
  key?: string;
  /** Who may open it; everyone when absent. */
  show?: (me: Me) => boolean;
  /** Whether the sidebar lists it (default: whenever `show` allows); the palette and `g` shortcuts still offer it. */
  inSidebar?: (me: Me) => boolean;
  /** Whether the current location is this page (defaults to the path and its sub-paths). */
  active?: (path: string, search: URLSearchParams) => boolean;
};

const editors = (me: Me) => me.role !== "viewer";
/** Contracts and licence pools need the `contracts` capability. */
const contracts = (me: Me) => me.capabilities.includes("contracts");
const admins = (me: Me) => me.role === "admin";
const webhooks = (me: Me) => admins(me) && me.capabilities.includes("webhooks");
const under = (prefix: string) => (path: string) => path === prefix || path.startsWith(`${prefix}/`);

export const NAV: NavItem[] = [
  // Everyone's home page, so it leads Work for every role; blocks the edition lacks say so.
  { id: "dashboard", label: "Dashboard", to: "/dashboard", icon: Gauge, section: "Work", key: "b" },
  { id: "attention", label: "Needs attention", to: "/", icon: Inbox, section: "Work", key: "n", active: (p) => p === "/" },
  { id: "data-quality", label: "Data quality", to: "/data-quality", icon: BarChart3, section: "Work", key: "q", show: (me) => me.capabilities.includes("data_quality") },
  { id: "assessments", label: "Change assessments", to: "/assessments", icon: ClipboardCheck, section: "Work", key: "h" },
  { id: "applications", label: "Applications", to: "/applications", icon: LayoutGrid, section: "Catalog", key: "a",
    active: (p, s) => (p === "/applications" && s.get("kind") !== "components") || (under("/applications")(p) && p !== "/applications" && !(p === "/applications/new" && s.get("kind") === "component")) },
  { id: "components", label: "Components", to: "/applications?kind=components", icon: Cog, section: "Catalog", key: "c",
    active: (p, s) => (p === "/applications" && s.get("kind") === "components") || under("/components")(p) || (p === "/applications/new" && s.get("kind") === "component") },
  { id: "agents", label: "AI agents", to: "/ai-agents", icon: Bot, section: "Catalog", key: "e" },
  { id: "integrations", label: "Integrations", to: "/integrations", icon: ArrowLeftRight, section: "Catalog", key: "i" },
  { id: "dependencies", label: "Dependencies", to: "/dependencies", icon: Network, section: "Catalog", key: "d" },
  // Where classified data goes (every live integration as a flow, its risks, a graph); capability
  // `data_flows` (community), read by every role.
  { id: "data-flows", label: "Data flows", to: "/data-flows", icon: Waypoints, section: "Catalog", show: (me) => me.capabilities.includes("data_flows") },
  // Its own address (it was People & Teams' Vendors tab, which redirects here).
  { id: "vendors", label: "Vendors", to: "/vendors", icon: Building2, section: "Catalog", key: "v", active: under("/vendors") },
  // Every vendor's risk file (tiers, assurance, documents, gaps) and the documents register; capability
  // `vendor_risk` (community), read by every role.
  { id: "vendor-risk", label: "Vendor risk", to: "/vendor-risk", icon: ShieldAlert, section: "Catalog", show: (me) => me.capabilities.includes("vendor_risk"),
    active: under("/vendor-risk") },
  // The end-of-life calendar of tracked versions (endoflife.date); capability `eol` (community), every role.
  { id: "end-of-life", label: "End of life", to: "/end-of-life", icon: CalendarX2, section: "Catalog", show: (me) => me.capabilities.includes("eol") },
  { id: "people", label: "People & Teams", to: "/admin/people", icon: Users, section: "Catalog", key: "p",
    active: (p) => p === "/admin/people" || under("/people")(p) },
  // Records with their owners, flagged for an access review (every role reads).
  { id: "access-review", label: "Access review", to: "/access-review", icon: UserX, section: "Catalog" },
  // Reference data the catalog points at: read by everyone, written by Editors and Admins, so under Catalog
  // rather than the Admin-only section. AI models has tabs for its providers and the models-in-use report.
  { id: "ai-models", label: "AI models", to: "/ai-models", icon: BrainCircuit, section: "Catalog",
    active: (p) => under("/ai-models")(p) || under("/ai-providers")(p) },
  { id: "categories", label: "Software categories", to: "/categories", icon: FolderTree, section: "Catalog" },
  { id: "tags", label: "Tags", to: "/tags", icon: Tags, section: "Catalog" },
  { id: "contracts", label: "Contracts", to: "/contracts", icon: FileSignature, section: "Money", key: "k", show: contracts },
  { id: "renewals", label: "Renewals", to: "/renewals", icon: CalendarClock, section: "Money", key: "r" },
  // Spend, the renewals calendar (and its feed) and licence utilisation; the calendar works without contracts.
  { id: "reports", label: "Reports", to: "/reports", icon: FileBarChart, section: "Money", key: "x" },
  { id: "license-pools", label: "License pools", to: "/license-pools", icon: TicketCheck, section: "Money", show: contracts },
  { id: "discovery", label: "Discovery", to: "/discovery", icon: Radar, section: "Discovery", key: "s",
    active: (p) => p === "/discovery" || under("/discovery/connectors")(p) || under("/discovery/setup")(p) || p === "/discovery/permissions" },
  // The credentials connectors sign in with (Admin only, reads included) and every connector's runs.
  { id: "connections", label: "Connections", to: "/discovery/connections", icon: Plug, section: "Discovery", show: admins },
  { id: "discovery-runs", label: "Run history", to: "/discovery/runs", icon: ListChecks, section: "Discovery" },
  // Third-party apps from Entra sign-ins and consent grants (every role reads; capability discovery.entra).
  { id: "shadow-it", label: "Shadow IT", to: "/discovery/shadow-it", icon: EyeOff, section: "Discovery", show: (me) => me.capabilities.includes("discovery.entra") },
  // Each application's single sign-on, SCIM and MFA from Entra (every role reads; capability discovery.entra).
  { id: "sso-coverage", label: "SSO coverage", to: "/discovery/sso-coverage", icon: ShieldCheck, section: "Discovery", show: (me) => me.capabilities.includes("discovery.entra") },
  { id: "imports", label: "Imports", to: "/imports", icon: Upload, section: "Discovery", key: "m", show: editors },
  { id: "users", label: "Users & roles", to: "/admin/users", icon: UserCog, section: "Admin", key: "u", show: admins },
  { id: "organization", label: "Organization settings", to: "/admin/organization", icon: Settings, section: "Admin", key: "o", show: admins },
  // The organization's single sign-on settings (Admin only, reads included; capability `sso.oidc`).
  { id: "sign-in", label: "Single sign-on", to: "/admin/sign-in", icon: ShieldCheck, section: "Admin", show: (me) => admins(me) && me.capabilities.includes("sso.oidc") },
  // SCIM tokens, provisioned users and groups (Admin only, reads included; capability `scim`, from the optional `ee` package).
  { id: "provisioning", label: "Provisioning", to: "/admin/provisioning", icon: UserCheck, section: "Admin", show: (me) => admins(me) && me.capabilities.includes("scim") },
  // Every role can open it (read only); only Admins change custom fields, so it sits with the Admin pages.
  { id: "custom-fields", label: "Custom fields", to: "/custom-fields", icon: ListPlus, section: "Admin", show: (me) => admins(me) && me.capabilities.includes("custom_fields") },
  // Every role can open it (read only: the types label every link); only Admins change them.
  { id: "relationship-types", label: "Relationship types", to: "/relationship-types", icon: Link2, section: "Admin", show: admins },
  // Admin only, reads included (rules and the log name recipients' addresses).
  { id: "notification-rules", label: "Notification rules", to: "/admin/notification-rules", icon: BellRing, section: "Admin", show: admins },
  { id: "notification-channels", label: "Notification channels", to: "/admin/notification-channels", icon: Send, section: "Admin", show: admins },
  { id: "notification-log", label: "Notification log", to: "/admin/notification-log", icon: History, section: "Admin", show: admins },
  // Jira, ServiceNow and Halo ticket connections (Admin only, reads included; capability `ticket_links`).
  { id: "ticket-connections", label: "Ticket connections", to: "/admin/ticket-connections", icon: Ticket, section: "Admin", show: (me) => admins(me) && me.capabilities.includes("ticket_links") },
  { id: "import-sources", label: "Import sources", to: "/admin/import-sources", icon: FileInput, section: "Admin", show: admins },
  { id: "tokens", label: "API tokens", to: "/admin/tokens", icon: KeyRound, section: "Admin", key: "t", show: admins },
  // Webhook endpoints and their delivery log (Admin only, reads included; capability `webhooks`).
  { id: "webhooks", label: "Webhooks", to: "/admin/webhooks", icon: Webhook, section: "Admin", show: webhooks, active: (p) => p === "/admin/webhooks" },
  { id: "webhook-log", label: "Webhook log", to: "/admin/webhooks/deliveries", icon: Logs, section: "Admin", show: webhooks },
  // The catalog as YAML files: export, plan and apply (Admin only; capability `catalog_code`).
  { id: "catalog-code", label: "Catalog as code", to: "/admin/catalog-code", icon: FileCode, section: "Admin", show: (me) => admins(me) && me.capabilities.includes("catalog_code") },
  // Every role may open the API reference, and the palette offers it to everyone; the sidebar lists it with the Admin
  // pages for Admins only (the Admin section holds Admin pages), and the account menu has it for everyone.
  { id: "api-docs", label: "API reference", to: "/admin/api", icon: BookOpen, section: "Admin", inSidebar: admins },
  { id: "audit", label: "Audit log", to: "/admin/audit", icon: ScrollText, section: "Admin", key: "l", show: admins },
  // The assistant's settings and usage log (Admin only, reads included; capability `assistant`).
  { id: "assistant", label: "Assistant", to: "/admin/assistant", icon: Lightbulb, section: "Admin", show: (me) => admins(me) && me.capabilities.includes("assistant") },
  { id: "system", label: "System status", to: "/admin/system", icon: Activity, section: "Admin", key: "y", show: admins },
  // Every role may read the organization's limits; the sidebar lists it for Admins (the account menu has it
  // for everyone, as with the API reference).
  { id: "limits", label: "Usage and limits", to: "/admin/limits", icon: Scale, section: "Admin", inSidebar: admins },
  // The platform admin console, outside the organization (platform staff only; the account menu has it too).
  { id: "platform", label: "Platform console", to: "/platform", icon: Server, section: "Admin", show: (me) => !!me.platform_staff && !me.impersonation, inSidebar: () => false },
];

/** The pages this person may open, in order. The Dashboard leads the Work section for every role (it is the landing
 *  page); `/` stays Needs attention, second. */
export const navFor = (me: Me) => NAV.filter((n) => !n.show || n.show(me));
/** The pages the sidebar lists for this person. */
export const sidebarFor = (me: Me) => navFor(me).filter((n) => !n.inSidebar || n.inSidebar(me));

export function isActive(item: NavItem, path: string, search: URLSearchParams) {
  return item.active ? item.active(path, search) : under(item.to)(path);
}

/** Records the `c` shortcut and the palette's Actions create (Editors and Admins); `capability` hides one the
 *  organisation's edition lacks. */
export type CreateAction = { id: string; label: string; to: string; capability?: string };
export const CREATE_ACTIONS: CreateAction[] = [
  { id: "application", label: "Create application", to: "/applications/new" },
  { id: "component", label: "Create component", to: "/applications/new?kind=component" },
  { id: "ai_agent", label: "Create AI agent", to: "/ai-agents/new" },
  { id: "integration", label: "Create integration", to: "/integrations/new" },
  { id: "renewal", label: "Create renewal", to: "/renewals/new" },
  { id: "contract", label: "Create contract", to: "/contracts/new", capability: "contracts" },
  { id: "license_pool", label: "Create license pool", to: "/license-pools/new", capability: "contracts" },
];
/** The create actions this organisation offers. */
export const createActionsFor = (me: Me) => CREATE_ACTIONS.filter((a) => !a.capability || me.capabilities.includes(a.capability));

/** What `c` creates on the current page: the list's own kind, or null where no list is in context. */
export function createInContext(path: string, search: URLSearchParams): CreateAction | null {
  const by = (id: string) => CREATE_ACTIONS.find((a) => a.id === id)!;
  if (path === "/applications") return by(search.get("kind") === "components" ? "component" : "application");
  if (under("/components")(path)) return by("component");
  if (under("/applications")(path)) return by("application");
  if (under("/ai-agents")(path)) return by("ai_agent");
  if (under("/integrations")(path)) return by("integration");
  if (under("/renewals")(path)) return by("renewal");
  if (under("/contracts")(path)) return by("contract");
  if (under("/license-pools")(path)) return by("license_pool");
  if (path === "/assessments") return { id: "assessment", label: "Create change assessment", to: "/assessments/new" };
  return null;
}
