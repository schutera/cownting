import { describe, expect, it } from "vitest";
import { oneClick } from "./clicks";

describe("oneClick", () => {
  it("runs on a single click and on a keyboard activation", () => {
    let n = 0;
    const h = oneClick(() => (n += 1));
    h({ detail: 1 });
    h({ detail: 0 });
    expect(n).toBe(2);
  });

  it("ignores the second and later clicks of a double- or triple-click", () => {
    // The second click lands on the NEXT cow's button in the same slot.
    let n = 0;
    const h = oneClick(() => (n += 1));
    h({ detail: 1 });
    h({ detail: 2 });
    h({ detail: 3 });
    expect(n).toBe(1);
  });
});
