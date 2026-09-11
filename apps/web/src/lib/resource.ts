/**
 * Loading data for a page, once, with the two things a bare `useEffect` gets wrong.
 *
 * **Stale responses.** Two loads in flight can finish out of order, and the older one
 * would overwrite the newer. Each run marks the previous one abandoned, so only the
 * most recent result is ever written.
 *
 * **Unmounted writes.** Navigating away mid-request would otherwise set state on a
 * component that is gone.
 *
 * It also keeps `setState` out of the effect body, which is what React's
 * `set-state-in-effect` rule is pointing at: the state updates happen in the
 * promise's callback, after the fetch, rather than synchronously during the effect.
 */
"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "./api";

interface Resource<T> {
  data: T | null;
  error: string;
  loading: boolean;
  /** Re-run the loader. Awaited by callers that need the new data before continuing. */
  reload: () => Promise<void>;
  setError: (message: string) => void;
}

export function useResource<T>(
  load: () => Promise<T>,
  {
    enabled = true,
    fallback = "Could not load that.",
  }: { enabled?: boolean; fallback?: string } = {},
): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(enabled);
  const generation = useRef(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const reload = useCallback(async () => {
    const run = ++generation.current;
    try {
      const next = await load();
      if (!alive.current || run !== generation.current) return;
      setData(next);
      setError("");
    } catch (caught) {
      if (!alive.current || run !== generation.current) return;
      setError(caught instanceof ApiError ? caught.message : fallback);
    } finally {
      if (alive.current && run === generation.current) setLoading(false);
    }
  }, [load, fallback]);

  useEffect(() => {
    if (!enabled) return;
    // The rule is aimed at state set synchronously during an effect, which cascades
    // renders. Nothing here is synchronous: `reload` awaits the request and writes
    // state in the promise's callback, guarded against stale and unmounted runs.
    // Fetching on mount is what an effect is for, and this hook is the only place in
    // the app that does it.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload();
  }, [enabled, reload]);

  return { data, error, loading, reload, setError };
}
