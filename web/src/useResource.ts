// SPDX-License-Identifier: AGPL-3.0-only
import { useEffect, useRef, useState } from "react";
import { loadResourceOnce } from "./resourceCache";

// One loading / error / data contract for every read in the operator UI.

export type Resource<T> = { data: T | null; error: unknown; loading: boolean };

type Outcome<T> = { key: string; data: T | null; error: unknown };

const IDLE: Resource<never> = { data: null, error: null, loading: false };
const LOADING: Resource<never> = { data: null, error: null, loading: true };

export function useResource<T>(key: string, loader: () => Promise<T>, enabled = true): Resource<T> {
  const latestLoader = useRef(loader);
  const [outcome, setOutcome] = useState<Outcome<T> | null>(null);

  // Declared first so the request effect below always sees the newest loader.
  useEffect(() => {
    latestLoader.current = loader;
  });

  useEffect(() => {
    if (!enabled) return undefined;
    let current = true;
    loadResourceOnce(key, () => latestLoader.current()).then(
      (data) => {
        if (current) setOutcome({ key, data, error: null });
      },
      (error: unknown) => {
        if (current) setOutcome({ key, data: null, error });
      },
    );
    return () => {
      current = false;
      setOutcome(null);
    };
  }, [key, enabled]);

  if (!enabled) return IDLE;
  if (outcome === null || outcome.key !== key) return LOADING;
  return { data: outcome.data, error: outcome.error, loading: false };
}
