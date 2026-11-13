/** The OpenAPI schema rendered by Scalar (@scalar/api-reference-react, bundled from npm, never a CDN), in its own chunk
 *  loaded on first use, so it stays out of the first bundle. The page fetches the schema itself (the signed-in
 *  session, same origin) and hands Scalar the document, so Scalar requests nothing.
 *
 *  Everything in Scalar that would talk to a third party is off: telemetry (PostHog), the Scalar agent (on by default
 *  on localhost), the developer tools and their uploads, the API client and its proxy, the MCP button and the default
 *  fonts from fonts.scalar.com; Scalar's service addresses point nowhere. "Test request" is hidden as well: it would
 *  send requests from a separate client without the session's CSRF header, and scripts use tokens anyway. Colours come
 *  from the tokens (scalar-theme.css); the theme follows <html data-theme>. */
// First: zod's jitless switch must be set before Scalar's schemas are made (no eval probe under the CSP).
import "./zod-jitless";
import { useEffect, useMemo, useRef, useState } from "react";
import { ApiReferenceReact } from "@scalar/api-reference-react";
import "@scalar/api-reference-react/style.css";
import "./scalar-theme.css";

/** <html data-theme>, watched directly: the theme menu and the e2e suite's theme switch both change it. */
function useDocumentTheme() {
  const read = () => (document.documentElement.dataset.theme === "dark" ? "dark" : "light") as "dark" | "light";
  const [theme, setTheme] = useState(read);
  useEffect(() => {
    const observer = new MutationObserver(() => setTheme(read()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);
  return theme;
}

/** Nowhere: a request to it fails at once and leaves the machine for no one. */
const NOWHERE = "about:blank";

export default function ScalarReference({ schema }: { schema: Record<string, unknown> }) {
  const theme = useDocumentTheme();
  const box = useRef<HTMLDivElement>(null);
  const configuration = useMemo(() => ({
    content: schema,
    theme: "none" as const,
    layout: "modern" as const,
    withDefaultFonts: false,
    darkMode: theme === "dark",
    forceDarkModeState: theme,
    hideDarkModeToggle: true,
    hideClientButton: true,
    hideTestRequestButton: true,
    showDeveloperTools: "never" as const,
    telemetry: false,
    agent: { disabled: true },
    mcp: { disabled: true },
    documentDownloadType: "none" as const,
    isEditable: false,
    // The address belongs to the app's router: Scalar's hash routing rewrote the next page's #anchor (the guide's
    // sections) with its own after leaving the reference.
    urlRouting: false,
    persistAuth: false,
    // Ctrl/Cmd+K is the app's command palette; the reference's own search takes Ctrl/Cmd+I.
    searchHotKey: "i" as const,
    baseServerURL: window.location.origin,
    externalUrls: { dashboardUrl: NOWHERE, registryUrl: NOWHERE, proxyUrl: NOWHERE, apiBaseUrl: NOWHERE },
  }), [schema, theme]);

  // Scalar draws its own document title as an <h1>; the page already has one (one h1 per page).
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const demote = () => el.querySelectorAll("h1").forEach((h) => { h.setAttribute("role", "heading"); h.setAttribute("aria-level", "2"); });
    demote();
    const observer = new MutationObserver(demote);
    observer.observe(el, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, []);

  return (
    <div ref={box} className="sw-api-reference rounded-lg border bg-card" data-testid="api-reference">
      <ApiReferenceReact configuration={configuration} />
    </div>
  );
}
