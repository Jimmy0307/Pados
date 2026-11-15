// SPDX-License-Identifier: AGPL-3.0-only
// A minimal read hook for the product pages: load on mount and on demand, drop late answers, report a lost session.
import { useCallback, useEffect, useRef, useState } from "react";
import { isSessionLost } from "./identity";

export type Loaded<T> = { data: T | null; error: unknown; loading: boolean; reload: () => void };

export function useLoad<T>(load: (() => Promise<T>) | null, deps: unknown[], onSessionLost?: (error: unknown) => void): Loaded<T> {
  const [state, setState] = useState<{ data: T | null; error: unknown; loading: boolean }>({ data: null, error: null, loading: Boolean(load) });
  const ticket = useRef(0);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((value) => value + 1), []);
  useEffect(() => {
    if (!load) { setState({ data: null, error: null, loading: false }); return; }
    const mine = ++ticket.current;
    setState((previous) => ({ ...previous, loading: true }));
    load().then((data) => { if (mine === ticket.current) setState({ data, error: null, loading: false }); })
      .catch((error: unknown) => {
        if (mine !== ticket.current) return;
        setState({ data: null, error, loading: false });
        if (isSessionLost(error)) onSessionLost?.(error);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { ...state, reload };
}
