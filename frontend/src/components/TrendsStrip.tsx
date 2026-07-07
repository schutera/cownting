import { Card, SectionLabel } from "./ui";
import MiniTrend from "./MiniTrend";
import PostureTrend from "./PostureTrend";
import ShelterTrend from "./ShelterTrend";
import AreaTrends from "./AreaTrends";

/**
 * Time-series trends, relocated out of the right rail to sit under the day
 * timeline with the orthophoto. The right rail is now static aggregate KPIs;
 * everything that changes over the day lives here. The three per-camera
 * sparklines follow the selected camera; the by-area panel spans all areas.
 */
export default function TrendsStrip({
  camera,
  trunc = "hour",
}: {
  camera: string;
  trunc?: string;
}) {
  return (
    <Card className="p-5 mt-6">
      <div className="flex items-baseline justify-between mb-4">
        <SectionLabel>TRENDS</SectionLabel>
        <span className="text-[12px] text-gray-tertiary">
          <span className="text-gray-mid">{camera}</span> ·{" "}
          {trunc === "hour" ? "today, hourly" : "by day"}
        </span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-x-6 gap-y-5">
        <MiniTrend camera={camera} trunc={trunc} />
        <PostureTrend camera={camera} trunc={trunc} />
        <ShelterTrend camera={camera} trunc={trunc} />
      </div>

      <div className="h-px bg-border my-5" />

      <AreaTrends trunc={trunc} />
    </Card>
  );
}
