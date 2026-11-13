/** TanStack Query on top of `api()`. api() keeps CSRF, problem errors and Retry-After; this file
 *  adds caching, request de-duplication and invalidation after writes.
 *
 *  Query key rule ("strict path", from useLoad): the key is the exact API path plus its parameters, so a
 *  screen never shows one path's data under another. `apiKey("applications?kind=components")` is
 *  `["api", "applications", { kind: "components" }]`: path segments first, then the parameters (sorted, so their order
 *  in the string doesn't matter). Because segments come first, `invalidate("applications")` refreshes every query
 *  under `applications/…`, whatever its parameters. */
import { QueryClient, useMutation, useQuery, useQueryClient, type QueryKey, type UseQueryOptions } from "@tanstack/react-query";
import { api, Problem } from "@/api";

export type ApiKey = readonly (string | Record<string, string>)[];

export function apiKey(path: string): ApiKey {
  const [base, search = ""] = path.split("?", 2);
  const segments = base.split("/").filter(Boolean);
  const params = new URLSearchParams(search);
  if (![...params.keys()].length) return ["api", ...segments];
  const sorted: Record<string, string> = {};
  // Repeated parameters (?tag=a&tag=b) keep every value, in order.
  for (const k of [...new Set(params.keys())].sort()) sorted[k] = params.getAll(k).join("\u0000");
  return ["api", ...segments, sorted];
}

/** Don't retry what the server answered (4xx: validation, permission, not found, throttled; 500: a bug that a second
 *  try won't fix, and the screen shows Retry). A dropped connection or a gateway error (502, 503, 504) gets one more
 *  try. */
const TRANSIENT = new Set([502, 503, 504]);
export function shouldRetry(failureCount: number, error: unknown) {
  if (failureCount >= 1) return false;
  return error instanceof Problem ? TRANSIENT.has(error.status) : true;
}

export function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetry,
        retryDelay: 500,
        // No refetch storms: data is refreshed when a screen mounts or a write invalidates it, not on every focus.
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
        staleTime: 0,
      },
      mutations: { retry: false },
    },
  });
}

export const queryClient = makeQueryClient();

/** GET an API path through the cache. `path` is relative to /api/v1/, as for api().
 *
 *  `abortWhenUnused`: pass TanStack Query's signal, so the request is aborted when no screen shows the query any more
 *  (searches as you type). Not the default: under React's StrictMode (development and the e2e suite) every screen
 *  mounts twice, so every first request would be aborted and sent again, and each abort drops a proxied connection;
 *  in the e2e run that exhausted Postgres connections (the test API keeps one per server thread). */
export function useApiQuery<T>(path: string, { abortWhenUnused = false, ...options }: Omit<UseQueryOptions<T, Error, T, QueryKey>, "queryKey" | "queryFn"> & { abortWhenUnused?: boolean } = {}) {
  return useQuery<T, Error, T, QueryKey>({
    queryKey: apiKey(path),
    queryFn: abortWhenUnused ? ({ signal }) => api<T>(path, { signal }) : () => api<T>(path),
    ...options,
  });
}

/** A user-facing message for a failed query or mutation. */
export function problemText(error: unknown, fallback = "Something went wrong. Try again.") {
  if (!(error instanceof Problem)) return error ? fallback : "";
  return plainProblem(error);
}

/** The framework's own sentences ("Request was throttled. Expected available in 42 seconds.", "You do not have
 *  permission to perform this action.") said in the app's words. Softwarden's own refusals keep their text. */
const FRAMEWORK_FORBIDDEN = "You do not have permission to perform this action.";
export function plainProblem(error: Problem) {
  // Only the framework's throttle sentence: Softwarden's own 429s (an API token's limits, a sign-in limit) already say
  // what happened in words and keep them.
  if (error.status === 429 && error.detail.startsWith("Request was throttled")) {
    const wait = error.retryAfter;
    const when = !wait ? "in a minute" : wait < 90 ? `in ${wait} ${wait === 1 ? "second" : "seconds"}` : `in about ${Math.ceil(wait / 60)} minutes`;
    return `Too many requests in a short time, so Softwarden paused yours. Try again ${when}; nothing was changed.`;
  }
  if (error.status === 403 && error.detail === FRAMEWORK_FORBIDDEN) {
    return "Your role can't open or change this. An Admin of this organization can, or can give you the role that does.";
  }
  return error.detail;
}

/** A write through api(); on success every path in `invalidates` (and everything under it) is refetched. The mutation's
 *  variables are the request body unless `body` says otherwise; `path` may be built from them. Errors are api()'s
 *  Problem, so `attempt(() => m.mutateAsync(v))` gives field errors as before. */
export function useApiMutation<TVars = unknown, TResult = unknown>({ method = "POST", path, body, invalidates = [] }: {
  method?: "POST" | "PUT" | "PATCH" | "DELETE";
  path: string | ((vars: TVars) => string);
  body?: (vars: TVars) => unknown;
  invalidates?: string[];
}) {
  const client = useQueryClient();
  return useMutation<TResult, Error, TVars>({
    mutationFn: (vars: TVars) => api<TResult>(typeof path === "function" ? path(vars) : path, { method, body: body ? body(vars) : method === "DELETE" ? undefined : vars }),
    onSuccess: async () => {
      await Promise.all(invalidates.map((p) => client.invalidateQueries({ queryKey: apiKey(p) })));
    },
  });
}

/** Refetch every query under each path, for writes that don't go through useApiMutation. */
export function useInvalidate() {
  const client = useQueryClient();
  return (...paths: string[]) => Promise.all(paths.map((p) => client.invalidateQueries({ queryKey: apiKey(p) })));
}
