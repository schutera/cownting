import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import type { DatasetRow } from "./types";
import { getDatasets, setDatasetParam } from "./api";

/**
 * Selected data-package (day) shared across the app. Held here (and mirrored into
 * the api module via setDatasetParam) so every /api call is scoped to the chosen
 * day without threading a param through each fetch. Picking a day remounts the
 * dashboard subtree (App keys it on `dataset`), so all child effects re-fetch.
 */
type DatasetCtx = {
  datasets: DatasetRow[];
  dataset: string | null; // selected dataset_id; null until resolved / none exist
  setDataset: (id: string) => void;
  refresh: () => Promise<DatasetRow[]>; // re-fetch the day list (e.g. after an upload)
  loaded: boolean;
};

const Ctx = createContext<DatasetCtx | null>(null);

export function DatasetProvider({ children }: { children: ReactNode }) {
  const [datasets, setDatasets] = useState<DatasetRow[]>([]);
  const [dataset, setDatasetState] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    getDatasets()
      .then((rows) => {
        if (!alive) return;
        setDatasets(rows);
        if (rows.length) {
          const latest = rows[0].dataset_id; // API returns newest day first
          setDatasetParam(latest);
          setDatasetState(latest);
        }
        setLoaded(true);
      })
      .catch(() => {
        // No datasets dimension yet (pre-migration DB) — leave dataset null so the
        // backend serves whole-DB, exactly as before this feature.
        if (alive) setLoaded(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  function setDataset(id: string) {
    setDatasetParam(id); // update the api module BEFORE the remount re-fetches
    setDatasetState(id);
  }

  async function refresh(): Promise<DatasetRow[]> {
    const rows = await getDatasets().catch(() => [] as DatasetRow[]);
    setDatasets(rows);
    return rows;
  }

  return (
    <Ctx.Provider value={{ datasets, dataset, setDataset, refresh, loaded }}>
      {children}
    </Ctx.Provider>
  );
}

export function useDataset(): DatasetCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useDataset must be used within a DatasetProvider");
  return v;
}
