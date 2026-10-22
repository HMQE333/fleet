import { useCallback, useEffect, useRef, useState } from "react";
import { onEvent, type FleetEvent } from "./api";

/** Hash routes: #/ , #/tasks/<id> , #/inbox , #/profiles */
export function useRoute(): string[] {
  const read = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const [parts, setParts] = useState(read);
  useEffect(() => {
    const onHash = () => setParts(read());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return parts;
}

export function go(path: string) {
  window.location.hash = path;
}

/** Load data, then reload whenever a live event matches `filter` (debounced). */
export function useLive<T>(load: () => Promise<T>, filter: (ev: FleetEvent) => boolean, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const loadRef = useRef(load);
  loadRef.current = load;

  const reload = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    setData(null);
    reload();
    const off = onEvent((ev) => {
      if (!filter(ev)) return;
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(reload, 250);
    });
    const poll = window.setInterval(reload, 15000); // safety net if the stream drops
    return () => {
      off();
      window.clearInterval(poll);
      window.clearTimeout(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, reload };
}

/** Re-render every `ms` so relative times and timers stay fresh. */
export function useTick(ms = 1000) {
  const [, setN] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => setN((n) => n + 1), ms);
    return () => window.clearInterval(t);
  }, [ms]);
}
