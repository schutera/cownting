import { useDataset } from "../lib/dataset";
import { Chip } from "./ui";

/**
 * Header day / data-package selector, styled with the app's own Chip pills (the
 * same control as the camera / hourly-daily toggles) rather than a native select.
 * Renders only when there is a package to pick — a pre-migration DB has none.
 */
export function DatasetPicker() {
  const { datasets, dataset, setDataset } = useDataset();
  if (datasets.length === 0) return null;
  return (
    <div className="flex items-center gap-2">
      <span className="text-[11px] font-mono uppercase tracking-[0.16em] text-gray-tertiary">
        Day
      </span>
      <div className="flex items-center gap-1.5 flex-wrap">
        {datasets.map((d) => (
          <Chip
            key={d.dataset_id}
            active={dataset === d.dataset_id}
            onClick={() => setDataset(d.dataset_id)}
          >
            {d.label ?? d.day ?? d.dataset_id}
          </Chip>
        ))}
      </div>
    </div>
  );
}
