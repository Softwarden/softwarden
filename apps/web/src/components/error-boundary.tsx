/** Error boundaries: a render exception shows a recoverable error state instead of blanking the application.
 *  `AppErrorBoundary` wraps the whole app (main.tsx); `PageErrorBoundary` wraps the shell's pages (App.tsx), so the
 *  sidebar and header survive a page that throws, and moving to another address clears the error. */
import { Component, useEffect, useRef, type ErrorInfo, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import { Button } from "@/components/ui/button";

type BoundaryProps = { children: ReactNode; fallback: ReactNode; resetKey?: string };

/** Catches a throw from its children's render and shows `fallback`; a new `resetKey` (the address) tries again. */
class ErrorBoundary extends Component<BoundaryProps, { failed: boolean; key?: string }> {
  state: { failed: boolean; key?: string } = { failed: false, key: this.props.resetKey };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  static getDerivedStateFromProps(props: BoundaryProps, state: { failed: boolean; key?: string }) {
    // Another address: show its page again, whatever happened on the last one.
    if (props.resetKey !== state.key) return { failed: false, key: props.resetKey };
    return null;
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    // Kept for whoever opens the console; nothing is sent anywhere.
    console.error("Softwarden: a page failed to render.", error, info.componentStack);
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

const reload = () => window.location.reload();

/** The error state inside the shell: the page's heading, why, and two ways on. Its heading takes focus when it
 *  appears, as a new page's would, so keyboard and screen reader users land on what replaced the page. */
function PageCrashed() {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    document.title = "Something went wrong · Softwarden";
    heading.current?.focus();
  }, []);
  return (
    <>
      <div className="mb-5"><h1 ref={heading} tabIndex={-1} className="font-display text-title font-bold text-text-strong outline-none">Something went wrong on this page</h1></div>
      <div className="max-w-prose rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert">
        <p className="font-semibold"><span aria-hidden="true">! </span>This page couldn't be shown.</p>
        <p className="mt-1">Nothing you saved is affected, and the rest of Softwarden still works. Reload the page to try again; if it keeps happening, tell your administrator which page it was.</p>
        <div className="mt-3 flex flex-wrap gap-2">
          <Button type="button" onClick={reload}>Reload the page</Button>
          <Button asChild variant="outline"><Link to="/">Go to Needs attention</Link></Button>
        </div>
      </div>
    </>
  );
}

/** The shell's pages, cleared by any navigation: the location's key is new for every one, the same address or only a
 *  new query included (a sidebar link to the page that failed, or Go to Needs attention when it was Needs attention).
 *  Healthy pages aren't remounted by it; only the error state is cleared. */
export function PageErrorBoundary({ children }: { children: ReactNode }) {
  const { key } = useLocation();
  return <ErrorBoundary resetKey={key} fallback={<PageCrashed />}>{children}</ErrorBoundary>;
}

/** The error state when the application itself fails (outside any page): no shell to keep, so a page of its own. A
 *  plain link, not the router's, because the router may be what failed. */
function AppCrashed() {
  useEffect(() => { document.title = "Something went wrong · Softwarden"; }, []);
  return (
    <main id="main" className="grid min-h-screen place-items-center bg-background p-4">
      <div className="w-full max-w-md rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert">
        <h1 className="font-display text-title font-bold text-text-strong"><span aria-hidden="true">! </span>Something went wrong</h1>
        <p className="mt-1">Softwarden couldn't show this page. Nothing you saved is affected. Reload the page to try again; if it keeps happening, tell your administrator.</p>
        <div className="mt-3 flex flex-wrap gap-2">
          <Button type="button" onClick={reload}>Reload the page</Button>
          <Button asChild variant="outline"><a href="/">Go to Needs attention</a></Button>
        </div>
      </div>
    </main>
  );
}

/** The whole application. */
export function AppErrorBoundary({ children }: { children: ReactNode }) {
  return <ErrorBoundary fallback={<AppCrashed />}>{children}</ErrorBoundary>;
}
