import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

const EXPERT_MODE_KEY = "ki.expertMode";
const EXPERT_MODE_EVENT = "ki:expert-mode";

// "Service links" in the topbar. Off, the console hides every deep link into the
// component dashboards (Hatchet, OpenSearch, Langfuse, LiteLLM) and the API docs, so
// the everyday admin surface stays self-contained; on, they appear on the pipeline,
// models and activity pages. The hook and its storage key keep the older "expert"
// name because both are read from several pages. Persisted in localStorage and shared
// across components via a window event so every mounted hook stays in sync.
export function useExpertMode() {
  const [expert, setExpert] = useState(() => localStorage.getItem(EXPERT_MODE_KEY) === "1");

  useEffect(() => {
    const sync = () => setExpert(localStorage.getItem(EXPERT_MODE_KEY) === "1");
    window.addEventListener(EXPERT_MODE_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(EXPERT_MODE_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);

  const setExpertMode = useCallback((value) => {
    if (value) localStorage.setItem(EXPERT_MODE_KEY, "1");
    else localStorage.removeItem(EXPERT_MODE_KEY);
    window.dispatchEvent(new Event(EXPERT_MODE_EVENT));
  }, []);

  return [expert, setExpertMode];
}

export function useApi(path, deps = [], enabled = true) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState(null);

  const reload = useCallback(async () => {
    if (!enabled || !path) return null;
    setLoading(true);
    setError(null);
    try {
      const result = await api(path);
      setData(result);
      return result;
    } catch (caught) {
      setError(caught);
      throw caught;
    } finally {
      setLoading(false);
    }
  }, [path, enabled]);

  useEffect(() => {
    reload().catch(() => {});
  }, [reload, ...deps]);

  return { data, loading, error, reload, setData };
}
