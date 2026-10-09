# Contract: `web/src/useResource.ts`

## Purpose

A React hook giving every read one loading/error/data contract.

## API

```ts
export type Resource<T> = { data: T | null; error: unknown; loading: boolean };
export function useResource<T>(key: string, loader: () => Promise<T>, enabled?: boolean /* default true */): Resource<T>
```

## Behaviour

1. **Loading.** Requests go through `loadResourceOnce(key, loader)`, so concurrent consumers of the same key share one request.
2. **Disabled.** While `enabled` is false, return `{data: null, error: null, loading: false}` and issue no request.
3. **Initial state.** When enabled, the state is `{data: null, error: null, loading: true}` until the load for the current key settles.
4. **Success and failure.** Success gives `{data, error: null, loading: false}`; failure gives `{data: null, error, loading: false}`.
5. **Key change.** When `key` changes, report `{data: null, error: null, loading: true}` until the *new* key settles. Data from the previous key must never be shown under the new key.
6. **Late results.** A response that arrives after unmount or after a key change is ignored.
7. **Loader identity.** A changed loader identity alone does not trigger a new request; the latest loader is used on the next key or enable change.

**Consumer:** `web/src/control-center.tsx` (`useResource<T>(path, () => getJson<T>(path))`, reading `data`, `loading` and `error`).

**Checks:** `npm run typecheck`, `npm test`, `npm run lint`, Playwright suites.
