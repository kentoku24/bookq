/**
 * bookq typed API client surface (#236).
 *
 * The single typed seam between the PWA and `/api/v1`: request and response
 * shapes come straight from the generated `api-types.d.ts`, so a spec change
 * that renames a field breaks `tsc` here (and at the typed call sites)
 * instead of surfacing only in Playwright E2E.
 *
 * Implemented in `app/static/api/v1.js` (plain script, `// @ts-check`);
 * this file only declares the surface so every `checkJs` file in scope
 * (`jsconfig.json`) reads the same types. `Window` carries an open index
 * signature: legacy cross-file globals (`FolderVisibility`, `OfflineCore`,
 * …) stay `any` while `BookqApi` itself is precise.
 */
import type { components, operations } from './api-types';

export type TreeNode = components['schemas']['TreeNode'];
export type TreeResponse = components['schemas']['TreeResponse'];
export type Facets = components['schemas']['Facets'];
export type ComicMetadata = components['schemas']['ComicMetadata'];
export type ReadingPosition = components['schemas']['ReadingPosition'];
export type FolderVisibility = components['schemas']['FolderVisibility'];
export type ApiErrorBody = components['schemas']['Error'];

export type TreeQuery = NonNullable<operations['getTree']['parameters']['query']>;
export type SearchQuery = NonNullable<operations['search']['parameters']['query']>;
export type FacetsQuery = NonNullable<operations['getFacets']['parameters']['query']>;
export type PositionsQuery = NonNullable<
  operations['listReadingPositions']['parameters']['query']
>;
export type DeletePositionsQuery = NonNullable<
  operations['deleteReadingPosition']['parameters']['query']
>;
export type PutPositionBody = NonNullable<
  NonNullable<operations['putReadingPosition']['requestBody']>['content']['application/json']
>;
export type PutPositionResult = NonNullable<
  operations['putReadingPosition']['responses'][200]['content']['application/json']
>;
export type PositionsResult = NonNullable<
  operations['listReadingPositions']['responses'][200]['content']['application/json']
>;
export type SearchResult = NonNullable<
  operations['search']['responses'][200]['content']['application/json']
>;
export type ImagesResult = NonNullable<
  operations['listImages']['responses'][200]['content']['application/json']
>;
export type PutVisibilityBody = NonNullable<
  NonNullable<operations['putFolderVisibility']['requestBody']>['content']['application/json']
>;

export interface FetchOpts {
  fetch?: typeof fetch;
  signal?: AbortSignal | null;
}

export interface RetryOpts extends FetchOpts {
  maxAttempts?: number;
  isCurrent?: (() => boolean) | null;
  init?: RequestInit;
}

export interface ApiErrorShape extends Error {
  status: number;
  code: string | null;
  detail: string;
}

export interface BookqApiShape {
  endpoints: { [key: string]: string };
  ApiError: new (status: number, code: string | null, detail: string) => ApiErrorShape;

  treeUrl(opts?: { path?: string; depth?: number; since?: number | null }): string;
  imagesUrl(folder: string): string;
  putPositionUrl(folder: string): string;

  nodesOf(body: unknown): TreeNode[] | null;
  imagesOf(body: unknown): string[] | null;
  latestPositionOf(body: unknown): ReadingPosition | null;
  errorCodeOf(body: unknown): string | null;

  fetchJson(
    url: string,
    init?: RequestInit,
    fetchFn?: typeof fetch,
  ): Promise<{ status: number; body: unknown; headers: Headers; res: Response }>;
  fetchWithBuildingRetry(
    url: string,
    fetchFn?: typeof fetch,
    opts?: RetryOpts,
  ): Promise<Response>;

  getTree(params?: TreeQuery, opts?: RetryOpts): Promise<TreeResponse>;
  getImages(folder: string, opts?: FetchOpts): Promise<string[]>;
  search(params: SearchQuery, opts?: RetryOpts): Promise<SearchResult>;
  getFacets(params?: FacetsQuery, opts?: RetryOpts): Promise<Facets>;
  getPositions(query?: PositionsQuery, opts?: FetchOpts): Promise<ReadingPosition[]>;
  putPosition(rec: PutPositionBody & { folder: string }, opts?: FetchOpts): Promise<PutPositionResult>;
  deletePositions(query?: DeletePositionsQuery, opts?: FetchOpts): Promise<boolean>;
  getVisibility(opts?: FetchOpts): Promise<FolderVisibility>;
  putVisibility(root: string, hidden: boolean, owner: string, opts?: FetchOpts): Promise<FolderVisibility>;
}

declare global {
  // eslint-disable-next-line no-var
  var BookqApi: BookqApiShape;
  // eslint-disable-next-line no-var
  var BookqApiEndpoints: { [key: string]: string };
  interface Window {
    BookqApi: BookqApiShape;
    // Legacy cross-file globals stay untyped; BookqApi above stays precise.
    // (Index signatures must be compatible with every named member — `any`
    // absorbs them all.)
    [key: string]: any;
  }
}

export {};
