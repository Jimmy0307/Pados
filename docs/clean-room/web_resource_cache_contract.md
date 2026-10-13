# Contract: `web/src/resourceCache.ts`

## Purpose

In-flight request de-duplication for read requests. **Not a data cache**: resolved values are never retained.

## API

```ts
export function loadResourceOnce<T>(key: string, loader: () => Promise<T>): Promise<T>
```

## Behaviour

- **Sharing.** While a promise for `key` is pending, further calls with the same key return **the same promise object**, and the loader is not called again.
- **Release.** When that promise settles (resolve or reject), the key is released, so the next call invokes a new loader. This means no stale values and retry after failure.
- **Isolation.** Different keys never share promises or values.
- **Replacement.** Releasing a key must not remove a newer promise registered later for the same key.

**Test:** `web/src/resourceCache.node-test.mjs` (run with `node --test`; the file is imported as `./resourceCache.ts`, so it must be plain, type-strippable TypeScript).
