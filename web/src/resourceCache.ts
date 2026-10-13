// SPDX-License-Identifier: AGPL-3.0-only
// Shares one in-flight promise per key between concurrent readers.
// Settled values are never kept: once a request finishes, the next read for
// that key starts a fresh request, so there are no stale values and a failed
// read can simply be retried.

const pending = new Map<string, Promise<unknown>>();

export function loadResourceOnce<T>(key: string, loader: () => Promise<T>): Promise<T> {
  const current = pending.get(key);
  if (current) return current as Promise<T>;

  let request: Promise<T>;
  try {
    request = Promise.resolve(loader());
  } catch (error) {
    request = Promise.reject(error);
  }
  const release = (): void => {
    // Only drop the entry if it still belongs to this request.
    if (pending.get(key) === request) pending.delete(key);
  };
  request.then(release, release);
  pending.set(key, request);
  return request;
}
