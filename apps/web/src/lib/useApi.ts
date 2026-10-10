"use client";

import {
  useCallback,
  useEffect,
  useEffectEvent,
  useRef,
  useState,
} from "react";
import { ApiError } from "@/lib/api/client";

/**
 * The workstation's whole data-fetching layer: two small hooks over the
 * typed endpoint functions. No polling, no background refetch, no
 * cross-view cache (a stale intelligence view is worse than a refetch).
 * Shared data (the repository overview) is fetched once by the shell and
 * provided through context instead of being re-requested per view.
 */

export type ResourceState<T> =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "success"; data: T }
  | { status: "error"; error: ApiError };

function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  return new ApiError(
    "malformed_response",
    "Unexpected error while handling the API response.",
    null,
  );
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

type Settled<T> = { requestKey: string; state: ResourceState<T> };

/**
 * Loads `fetcher` whenever `key` changes (pass `null` to stay idle).
 * `reload()` re-runs it on demand. Loading is derived (the latest settled
 * result belongs to an older request), and in-flight requests are aborted
 * when the key changes or the component unmounts, so a slow response can
 * never overwrite a newer one.
 */
export function useResource<T>(
  key: string | null,
  fetcher: (signal: AbortSignal) => Promise<T>,
): ResourceState<T> & { reload: () => void } {
  const [generation, setGeneration] = useState(0);
  const [settled, setSettled] = useState<Settled<T> | null>(null);
  const requestKey = key === null ? null : `${key}#${generation}`;
  const load = useEffectEvent((signal: AbortSignal) => fetcher(signal));

  useEffect(() => {
    if (requestKey === null) return;
    const controller = new AbortController();
    load(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) {
          setSettled({ requestKey, state: { status: "success", data } });
        }
      })
      .catch((error: unknown) => {
        if (isAbort(error) || controller.signal.aborted) return;
        setSettled({
          requestKey,
          state: { status: "error", error: toApiError(error) },
        });
      });
    return () => controller.abort();
  }, [requestKey]);

  const reload = useCallback(() => setGeneration((g) => g + 1), []);

  let state: ResourceState<T>;
  if (requestKey === null) state = { status: "idle" };
  else if (settled === null || settled.requestKey !== requestKey)
    state = { status: "loading" };
  else state = settled.state;
  return { ...state, reload };
}

/**
 * A user-triggered request (POST actions such as running an analysis).
 * Starting a new run aborts the previous one; nothing runs on mount.
 */
export function useAction<A extends unknown[], T>(
  action: (signal: AbortSignal, ...args: A) => Promise<T>,
): ResourceState<T> & { run: (...args: A) => void; reset: () => void } {
  const [state, setState] = useState<ResourceState<T>>({ status: "idle" });
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => () => controllerRef.current?.abort(), []);

  const run = (...args: A) => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setState({ status: "loading" });
    action(controller.signal, ...args)
      .then((data) => {
        if (!controller.signal.aborted) setState({ status: "success", data });
      })
      .catch((error: unknown) => {
        if (isAbort(error) || controller.signal.aborted) return;
        setState({ status: "error", error: toApiError(error) });
      });
  };

  const reset = () => {
    controllerRef.current?.abort();
    setState({ status: "idle" });
  };

  return { ...state, run, reset };
}
