import { describe, expect, it } from "vitest";
import { ringFor } from "./InstanceCrop";
import type { LabelItem } from "../lib/types";

/* `ringFor` decides which box is drawn on the crop, and it is the rule that
   REGRESSED twice while this feature was built — once by being fed a full-frame
   polygon into a crop-local viewBox (the overlay vanished off-canvas), and once
   by leaving the detector's box ringed after the annotator had already corrected
   the outline. Both were invisible failures: nothing threw, the screen just
   quietly showed the wrong thing. Hence this file. */

const BASE: LabelItem = {
  instance_key: "k",
  dataset_id: "2026-07-03",
  day: "2026-07-03",
  camera_id: "camera_01",
  frame_file: "00000001.jpg",
  bbox: [100, 200, 300, 400],
  ordinal: 0,
  score: 0.9,
  frame_sig: null,
  crop_url: "/api/img/label-crop/camera_01/00000001.jpg",
  frame_url: "/api/img/label-frame/camera_01/00000001.jpg",
  crop_w: 100,
  crop_h: 100,
  ring: [20, 25, 80, 75],
  n_annotators: 0,
  target: 2,
  overlap: false,
  serve_event_id: 1,
};

describe("ringFor", () => {
  it("uses the detector's box when nothing has been corrected", () => {
    expect(ringFor(BASE)).toEqual([20, 25, 80, 75]);
    expect(ringFor({ ...BASE, mask_seed: "bbox", mask: null })).toEqual([20, 25, 80, 75]);
  });

  it("keeps the detector's box for the MODEL's own outline", () => {
    // The two agree to within a pixel anyway (the segmenter crops its mask to
    // its box), so redrawing from it would change what the ring MEANS on every
    // item for no gain.
    expect(
      ringFor({
        ...BASE,
        mask_seed: "model",
        mask: [[30, 30], [70, 32], [50, 70]],
      }),
    ).toEqual([20, 25, 80, 75]);
  });

  it("follows the annotator's own correction", () => {
    // After a correction the detector's box is no longer the best statement of
    // where the animal is — theirs is, and the questions that follow are asked
    // about the shape they just fixed.
    const ring = ringFor({
      ...BASE,
      mask_seed: "edit",
      mask: [[30, 35], [70, 33], [64, 68], [36, 66]],
    });
    expect(ring).toEqual([30, 33, 70, 68]);
  });

  it("ignores a degenerate correction rather than collapsing the ring", () => {
    // Fewer than three points is not a shape; drawing its "extent" would put a
    // zero-area ring on the crop and dim the whole animal out.
    for (const mask of [null, [], [[10, 10]], [[10, 10], [20, 20]]] as LabelItem["mask"][]) {
      expect(ringFor({ ...BASE, mask_seed: "edit", mask })).toEqual([20, 25, 80, 75]);
    }
  });

  it("stays in CROP-LOCAL space — the space InstanceCrop's viewBox draws in", () => {
    // The regression this file exists for: `mask` must be the crop-local
    // projection. Feeding it the full-frame one puts the ring at coordinates far
    // outside a crop_w x crop_h viewBox, so it does not misdraw — it disappears.
    const ring = ringFor({
      ...BASE,
      mask_seed: "edit",
      mask: [[30, 35], [70, 33], [64, 68]],
    });
    expect(ring.every((v) => v >= 0 && v <= BASE.crop_w)).toBe(true);
  });
});
