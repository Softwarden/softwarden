/** The grouped sidebar: the organisation (a switcher when the account belongs to more than one),
 *  labelled sections (Work, Catalog, Money, Discovery, Admin; a section shows only with pages the role may open), the
 *  Needs attention badge for items routed to the signed-in user, and the user menu at the bottom.
 *
 *  At 1024 px and wider it can collapse to a 64 px rail of icons (`[` or the Collapse button), remembered per user in
 *  this browser; each icon keeps its name for screen readers and shows it as a tooltip on hover and focus. Below
 *  1024 px the sidebar is a "Menu" disclosure, as before.
 *
 *  Each section's label is a disclosure: a section someone closes stays closed in this browser, except
 *  the one holding the current page, which always shows. The rail shows every icon. */
import { useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { Building2, ChevronsLeft, ChevronsRight } from "lucide-react";
import type { Me, Membership } from "@/api";
import { MoreMenu } from "@/components/more-menu";
import { isActive, NAV_SECTIONS, sidebarFor, type NavItem, type NavSection } from "@/components/nav";
import { Avatar, BrandMark, Wordmark } from "@/components/sw";
import { prefKey, usePref } from "@/lib/prefs";
import { cn } from "@/lib/utils";

const NO_SECTIONS: NavSection[] = [];

/** A label shown beside a rail icon, positioned against the viewport so the sidebar's scroll box doesn't clip it. */
function useRailTip(rail: boolean) {
  const [tip, setTip] = useState<{ text: string; top: number } | null>(null);
  const show = (text: string) => (e: React.SyntheticEvent<HTMLElement>) => {
    if (!rail) return;
    const r = e.currentTarget.getBoundingClientRect();
    setTip({ text, top: r.top + r.height / 2 });
  };
  const hide = () => setTip(null);
  // Dismissible without moving the pointer or focus (WCAG 1.4.13): Esc hides it until the next hover or focus.
  useEffect(() => {
    if (!tip) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setTip(null); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [tip]);
  const node = rail && tip ? (
    <span aria-hidden="true" style={{ top: tip.top }}
      className="pointer-events-none fixed left-[calc(var(--rail-width)+6px)] z-50 hidden -translate-y-1/2 whitespace-nowrap rounded-md bg-popover px-2 py-1 text-sm text-popover-foreground shadow-md ring-1 ring-border lg:block">
      {tip.text}
    </span>
  ) : null;
  return { tipProps: (text: string) => ({ onMouseEnter: show(text), onFocus: show(text), onMouseLeave: hide, onBlur: hide }), node };
}

export function Sidebar({ me, rail, onToggleRail, badge, onSwitchOrganization, switching, onShowShortcuts, onSignOut, onProfile, onApiDocs, onLimits, onPlatform }: {
  me: Me;
  rail: boolean;
  onToggleRail: () => void;
  /** Needs attention items routed to the signed-in user. */
  badge: number;
  onSwitchOrganization: (org: Membership) => void;
  switching: boolean;
  onShowShortcuts: () => void;
  onSignOut: () => void;
  onProfile: () => void;
  /** The API reference, which every role may open. */
  onApiDocs: () => void;
  /** Usage and limits, which every role may open. */
  onLimits: () => void;
  /** The platform admin console: platform staff only, and not while impersonating. */
  onPlatform?: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  // The open phone menu closes on Escape, with focus back on its button, unless a menu inside it took the key.
  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented || document.querySelector('[role="menu"], [role="dialog"]')) return;
      setMenuOpen(false);
      menuButton.current?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [menuOpen]);
  const location = useLocation();
  const search = new URLSearchParams(location.search);
  const [closed, setClosed] = usePref<NavSection[]>(prefKey(me.user.id, "sidebar.closed"), NO_SECTIONS);
  const toggleSection = (section: NavSection) => setClosed(closed.includes(section) ? closed.filter((x) => x !== section) : [...closed, section]);
  const items = sidebarFor(me);
  const { tipProps, node: tip } = useRailTip(rail);
  const orgs = me.organizations ?? [];
  const roleName = me.role.charAt(0).toUpperCase() + me.role.slice(1);
  const linkClass = (active: boolean) => cn(
    "relative flex items-center gap-2.5 rounded-md px-2.5 py-2 text-[13.5px] no-underline transition-colors focus-visible:outline-sidebar-ring",
    rail && "lg:justify-center lg:px-0",
    active ? "bg-sidebar-accent font-semibold text-sidebar-accent-foreground shadow-[inset_3px_0_0_var(--sidebar-ring)]" : "text-sidebar-foreground hover:bg-sidebar-accent/60",
  );
  const label = cn(rail && "lg:sr-only");

  // A render function, not a component: a component declared here would remount every link on each render.
  function navLink(item: NavItem) {
    const active = isActive(item, location.pathname, search);
    const Icon = item.icon;
    const isAttention = item.id === "attention";
    return (
      <Link key={item.id} to={item.to} className={linkClass(active)} aria-current={active ? "page" : undefined} {...tipProps(item.label)}>
        <Icon aria-hidden="true" className="size-4 shrink-0 opacity-90" />
        <span className={label}>{item.label}</span>
        {isAttention && badge > 0 && (
          <span aria-hidden="true" className={cn("rounded-full bg-state-overdue px-1.5 text-[11px] font-semibold text-state-overdue-foreground", rail ? "ml-auto lg:absolute lg:right-1 lg:top-0.5 lg:ml-0" : "ml-auto")}>{badge}</span>
        )}
        {isAttention && <span className="sr-only">, {badge} items</span>}
      </Link>
    );
  }

  return (
    <div className="bg-sidebar text-sidebar-foreground lg:sticky lg:top-0 lg:flex lg:h-screen lg:flex-col lg:overflow-y-auto">
      <div className={cn("flex items-center gap-2.5 px-4 py-3 lg:py-4", rail && "lg:justify-center lg:px-2")}>
        <BrandMark />
        <Wordmark className={cn("text-[15px] text-sidebar-accent-foreground", label)} />
        <button type="button" ref={menuButton} className="ml-auto rounded-md border border-sidebar-border px-3 py-1.5 text-sm text-sidebar-foreground lg:hidden"
          aria-expanded={menuOpen} aria-controls="primary-nav" onClick={() => setMenuOpen(!menuOpen)}>Menu</button>
      </div>

      <div className={cn(menuOpen ? "flex" : "hidden", "flex-1 flex-col lg:flex")}>
        {/* The organisation: a switcher with more than one membership, otherwise its name. */}
        <div className={cn("px-2 pb-2", rail && "lg:px-1.5")}>
          {orgs.length > 1 ? (
            <div {...tipProps(`Organization: ${me.organization.name}`)}>
              <MoreMenu label={`Organization: ${me.organization.name}. Switch organization`} menuLabel="Switch organization" align="left" caret={!rail} disabled={switching} nameFromTrigger
                triggerClassName={cn("flex w-full items-center gap-2 rounded-md border border-sidebar-border px-2.5 py-2 text-left text-[13px] text-sidebar-foreground hover:bg-sidebar-accent/60 focus-visible:outline-sidebar-ring disabled:opacity-60", rail && "lg:justify-center lg:px-0")}
                trigger={<>
                  <Building2 aria-hidden="true" className="size-4 shrink-0" />
                  <span className={cn("min-w-0 flex-1", label)}>
                    <span className="block text-[11px] text-sidebar-muted">Organization<span className="sr-only">: </span></span>
                    <span className="block truncate font-semibold text-sidebar-accent-foreground">{switching ? "Switching…" : me.organization.name}<span className="sr-only">. Switch organization</span></span>
                  </span>
                </>}
                actions={orgs.map((o) => ({ label: `${o.name} (${o.role.charAt(0).toUpperCase() + o.role.slice(1)})`, checked: o.id === me.organization.id, onSelect: () => { if (o.id !== me.organization.id) onSwitchOrganization(o); } }))} />
            </div>
          ) : (
            <p className={cn("px-2.5 text-[12px] text-sidebar-muted", rail && "lg:sr-only")}>
              <span className="sr-only">Organization: </span>{me.organization.name}
            </p>
          )}
        </div>

        <nav id="primary-nav" aria-label="Primary" className={cn("px-2 pb-4", rail && "lg:px-1.5")} onClick={() => setMenuOpen(false)}>
          {NAV_SECTIONS.map((section) => {
            const list = items.filter((i) => i.section === section);
            if (!list.length) return null;
            // The current page's section always shows; the rail always shows every icon.
            const current = list.some((i) => isActive(i, location.pathname, search));
            const open = rail || current || !closed.includes(section);
            return (
              <div key={section} role="group" aria-labelledby={`nav-${section}`}>
                {/* The label names the group and opens or closes it; it never navigates, so a click on it keeps the menu open. */}
                <button type="button" id={`nav-${section}`} aria-expanded={open} aria-controls={`nav-${section}-links`} disabled={current}
                  // The rail shows every icon and hides the label: the toggle has nothing to do there, so it takes no Tab stop.
                  tabIndex={rail ? -1 : undefined}
                  onClick={(e) => { e.stopPropagation(); toggleSection(section); }}
                  className={cn("flex w-full items-center gap-1 rounded-md px-2.5 pb-1 pt-4 text-left text-xs font-semibold uppercase tracking-wide text-sidebar-muted hover:text-sidebar-foreground focus-visible:outline-sidebar-ring disabled:cursor-default disabled:hover:text-sidebar-muted", rail && "lg:sr-only")}>
                  <span aria-hidden="true" className="w-3">{open ? "▾" : "▸"}</span>{section}
                  {!open && <span className="ml-auto font-normal normal-case tracking-normal">{list.length}<span className="sr-only"> pages</span></span>}
                </button>
                {rail && <hr aria-hidden="true" className="mx-2 my-2 hidden border-sidebar-border lg:block" />}
                <div id={`nav-${section}-links`} hidden={!open}>{list.map(navLink)}</div>
              </div>
            );
          })}
        </nav>

        {/* User menu, then the rail toggle. */}
        <div className={cn("mt-auto flex flex-col gap-1 border-t border-sidebar-border px-2 py-3", rail && "lg:px-1.5")}>
          <div {...tipProps(`${me.user.name} · ${roleName}`)}>
            <MoreMenu label={`${me.user.name} · ${roleName}. Account`} menuLabel="Account" side="top" align="left" caret={false} nameFromTrigger
              triggerClassName={cn("flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-[13px] text-sidebar-foreground hover:bg-sidebar-accent/60 focus-visible:outline-sidebar-ring", rail && "lg:justify-center lg:px-0")}
              trigger={<>
                <Avatar name={me.user.name} />
                <span className={cn("min-w-0 flex-1", label)}>
                  <span className="block truncate font-semibold text-sidebar-accent-foreground">{me.user.name}<span className="sr-only"> · </span></span>
                  <span className="block text-[11px] text-sidebar-muted">{roleName}<span className="sr-only">. Account</span></span>
                </span>
              </>}
              actions={[
                { label: "Profile and notifications", onSelect: onProfile },
                { label: "Keyboard shortcuts", onSelect: onShowShortcuts },
                { label: "API reference", onSelect: onApiDocs },
                { label: "Usage and limits", onSelect: onLimits },
                ...(onPlatform ? [{ label: "Platform console", onSelect: onPlatform }] : []),
                { label: "Sign out", onSelect: onSignOut },
              ]} />
          </div>
          <button type="button" onClick={onToggleRail} aria-pressed={rail} aria-keyshortcuts="["
            className={cn("hidden items-center gap-2 rounded-md px-2.5 py-1.5 text-[12.5px] text-sidebar-muted hover:bg-sidebar-accent/60 hover:text-sidebar-foreground focus-visible:outline-sidebar-ring lg:flex", rail && "lg:justify-center lg:px-0")}
            {...tipProps(rail ? "Expand sidebar ([)" : "Collapse sidebar ([)")}>
            {rail ? <ChevronsRight aria-hidden="true" className="size-4" /> : <ChevronsLeft aria-hidden="true" className="size-4" />}
            <span className={label}>Collapse sidebar</span>
            <kbd aria-hidden="true" className={cn("ml-auto rounded-sm border border-sidebar-border px-1 font-mono text-[11px]", rail && "lg:hidden")}>[</kbd>
          </button>
        </div>
      </div>
      {tip}
    </div>
  );
}
