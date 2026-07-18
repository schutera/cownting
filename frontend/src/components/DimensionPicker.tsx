import { Chip } from "./ui";
import type { FeatureDef } from "../lib/features";

// A compact controls-row picker: one selectable Chip per feature. Unavailable
// features (available[key] === false) render muted with a "soon" hint and are
// not clickable. `allowNone` prepends a "None" chip that clears the dimension.
export function DimensionPicker({
  label,
  value,
  onChange,
  features,
  available,
  allowNone = false,
}: {
  label: string;
  value: string;
  onChange: (key: string) => void;
  features: FeatureDef[];
  available: Record<string, boolean>;
  allowNone?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
      <span className="text-[11px] font-mono uppercase tracking-[0.12em] text-gray-tertiary">
        {label}
      </span>
      <div className="flex flex-wrap items-center gap-1.5">
        {allowNone ? (
          <Chip active={value === ""} onClick={() => onChange("")}>
            None
          </Chip>
        ) : null}
        {features.map((f) => {
          const isAvailable = available[f.key] !== false;
          if (!isAvailable) {
            return (
              <span
                key={f.key}
                title="Not available yet"
                className="inline-flex items-center gap-1.5 text-[13px] px-3 py-1.5 rounded-full border border-border bg-surface text-gray-tertiary opacity-50 cursor-not-allowed"
              >
                {f.label}
                <span className="text-[9px] font-mono uppercase tracking-[0.1em]">soon</span>
              </span>
            );
          }
          return (
            <Chip key={f.key} active={value === f.key} onClick={() => onChange(f.key)}>
              {f.label}
            </Chip>
          );
        })}
      </div>
    </div>
  );
}
