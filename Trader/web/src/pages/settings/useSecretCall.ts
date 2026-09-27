// A one-shot API call for forms that send a secret (a Questrade token, a password, a two-step code).
//
// TanStack's `useMutation` keeps the argument it was called with as the mutation's `variables`, in the query
// client's mutation cache, until the mutation is garbage-collected: a secret passed to `mutate()` stays in
// memory (and in React Query devtools) after the form is cleared. This hook keeps only the outcome: the
// caller passes a thunk that closes over the secret, and only the result (which never contains it) or the
// error is kept in state. A second call while one is in flight is ignored.
import { useCallback, useEffect, useRef, useState } from "react";

type State<R> = { status: "idle" } | { status: "pending" } | { status: "success"; data: R } | { status: "error"; error: unknown };

export interface SecretCall<R> {
  isPending: boolean;
  isSuccess: boolean;
  isError: boolean;
  /** The last result, while `isSuccess`. */
  data: R | undefined;
  /** The last error, while `isError`. */
  error: unknown;
  /** Runs `call` unless one is already running; resolves to its result, or undefined on error or when skipped. */
  run(call: () => Promise<R>): Promise<R | undefined>;
  /** Forgets the last outcome. */
  reset(): void;
}

export function useSecretCall<R>(): SecretCall<R> {
  const [state, setState] = useState<State<R>>({ status: "idle" });
  const inFlight = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const run = useCallback(async (call: () => Promise<R>): Promise<R | undefined> => {
    if (inFlight.current) return undefined;
    inFlight.current = true;
    setState({ status: "pending" });
    try {
      const data = await call();
      if (mounted.current) setState({ status: "success", data });
      return data;
    } catch (error) {
      if (mounted.current) setState({ status: "error", error });
      return undefined;
    } finally {
      inFlight.current = false;
    }
  }, []);

  const reset = useCallback(() => setState({ status: "idle" }), []);

  return {
    isPending: state.status === "pending",
    isSuccess: state.status === "success",
    isError: state.status === "error",
    data: state.status === "success" ? state.data : undefined,
    error: state.status === "error" ? state.error : null,
    run,
    reset,
  };
}
