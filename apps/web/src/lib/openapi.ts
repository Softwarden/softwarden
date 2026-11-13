/** Typed calls from the generated OpenAPI types (acceptance check: a client generated from the schema, used by the
 *  SPA). `src/generated/openapi.d.ts` is written by `npm run api:types` from the API's own schema and committed; lint
 *  fails when it is stale (scripts/openapi-types.mjs --check). This file adds no runtime of its own: every call goes
 *  through `api()` (CSRF, problem details, Retry-After) and every read through `useApiQuery` (the shared cache and its
 *  invalidation), so a typed screen behaves exactly like the others. Only the types come from the schema.
 *
 *  `apiPath("webhooks/{id}/ping", { path: { id } })` builds the string `api()` takes, with the path parameters encoded
 *  and the query typed from the operation; `typedApi("post", "webhooks/{id}/ping", { path: { id } })` sends it and
 *  returns the operation's success body; `useTypedQuery("webhook-deliveries", { query })` reads one. A path, method,
 *  parameter or body field the schema doesn't have is a compile error. */
import type { components, paths } from "@/generated/openapi";
import { api, apiWithEtag } from "@/api";
import { useApiQuery } from "@/lib/query";
import type { QueryKey, UseQueryOptions } from "@tanstack/react-query";

export type Schemas = components["schemas"];

type PREFIX = "/api/v1/";
type Full = keyof paths;
/** A route as `api()` takes it: the schema's path without `/api/v1/`, placeholders kept ("webhooks/{id}"). */
export type ApiPath = Full extends `${PREFIX}${infer P}` ? P : never;
export type Method = "get" | "post" | "patch" | "put" | "delete";
type Item<P extends ApiPath> = paths[`${PREFIX}${P}`];
type Op<P extends ApiPath, M extends Method> = NonNullable<Item<P>[M]>;
/** The methods a route has. */
export type MethodOf<P extends ApiPath> = { [M in Method]: Item<P>[M] extends undefined ? never : M }[Method];

type Success = 200 | 201 | 202 | 204;
type Json<R> = R extends { content: { "application/json": infer J } } ? J : undefined;
type Responses<O> = O extends { responses: infer R } ? R : never;
/** The success body of an operation (200, 201, 202; undefined for 204). */
export type ResponseOf<P extends ApiPath, M extends MethodOf<P>> = Json<Responses<Op<P, M>>[Extract<keyof Responses<Op<P, M>>, Success>]>;
/** The JSON request body of an operation. */
export type BodyOf<P extends ApiPath, M extends MethodOf<P>> = Op<P, M> extends { requestBody?: never } ? never
  : Op<P, M> extends { requestBody?: { content: { "application/json": infer B } } } ? B : never;
type Params<P extends ApiPath, M extends MethodOf<P>> = Op<P, M> extends { parameters: infer X } ? X : never;
/** The query parameters of an operation. */
export type QueryOf<P extends ApiPath, M extends MethodOf<P>> = NonNullable<Params<P, M> extends { query?: infer Q } ? Q : never>;
type PathParamsOf<P extends ApiPath, M extends MethodOf<P>> = Params<P, M> extends { path: infer X } ? X : never;

type Values = Record<string, string | number | boolean | undefined | null>;
export type CallOptions<P extends ApiPath, M extends MethodOf<P>> =
  & ([PathParamsOf<P, M>] extends [never] ? { path?: never } : { path: PathParamsOf<P, M> })
  & { query?: Partial<QueryOf<P, M>>; headers?: Record<string, string>; signal?: AbortSignal }
  & ([BodyOf<P, M>] extends [never] ? { body?: never } : { body: BodyOf<P, M> });

/** The path `api()` takes: placeholders filled (encoded) and the defined query parameters appended, in their order. */
export function apiPath<P extends ApiPath, M extends MethodOf<P> = "get" & MethodOf<P>>(
  path: P, options: { path?: PathParamsOf<P, M>; query?: Partial<QueryOf<P, M>> } = {},
): string {
  const values = (options.path ?? {}) as Values;
  const filled = path.replace(/\{([^}]+)\}/g, (_, name: string) => {
    const value = values[name];
    if (value === undefined || value === null || value === "") throw new Error(`Missing path parameter ${name} for ${path}`);
    return encodeURIComponent(String(value));
  });
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries((options.query ?? {}) as Values)) {
    if (v !== undefined && v !== null && v !== "") search.set(k, String(v));
  }
  const query = search.toString();
  return query ? `${filled}?${query}` : filled;
}

/** Send one operation through `api()`; its success body, typed. Errors are api()'s `Problem`. */
export function typedApi<P extends ApiPath, M extends MethodOf<P>>(method: M, path: P, options: CallOptions<P, M>): Promise<ResponseOf<P, M>> {
  const { body, headers, signal } = options as { body?: unknown; headers?: Record<string, string>; signal?: AbortSignal };
  return api<ResponseOf<P, M>>(apiPath<P, M>(path, options as { path?: PathParamsOf<P, M>; query?: Partial<QueryOf<P, M>> }), {
    method: method.toUpperCase(),
    body: method === "get" ? undefined : body,
    headers,
    signal,
  });
}

type Get<P extends ApiPath> = "get" & MethodOf<P>;
/** `path` is required exactly when the route has placeholders. */
type PathPart<P extends ApiPath, M extends MethodOf<P>> = [PathParamsOf<P, M>] extends [never] ? { path?: never } : { path: PathParamsOf<P, M> };
type GetOptions<P extends ApiPath> = PathPart<P, Get<P>> & { query?: Partial<QueryOf<P, Get<P>>> };
type QueryOptions<P extends ApiPath> = Omit<UseQueryOptions<ResponseOf<P, Get<P>>, Error, ResponseOf<P, Get<P>>, QueryKey>, "queryKey" | "queryFn">;
/** The options argument may be left out only for a route without placeholders. */
type OptionsArg<P extends ApiPath, O, Rest extends unknown[] = []> = [PathParamsOf<P, Get<P>>] extends [never] ? [options?: O, ...Rest] : [options: O, ...Rest];

/** GET a record with its ETag, for an If-Match edit. */
export function typedGetWithEtag<P extends ApiPath>(path: P, ...[options]: OptionsArg<P, PathPart<P, Get<P>>>) {
  return apiWithEtag<ResponseOf<P, Get<P>>>(apiPath(path, (options ?? {}) as never));
}

/** `useApiQuery` with the operation's types: the same cache key (the built path) and invalidation as every screen. */
export function useTypedQuery<P extends ApiPath>(path: P, ...[options, queryOptions]: OptionsArg<P, GetOptions<P>, [queryOptions?: QueryOptions<P>]>) {
  return useApiQuery<ResponseOf<P, Get<P>>>(apiPath(path, (options ?? {}) as never), queryOptions ?? {});
}
