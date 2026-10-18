/* Clicks on the Label screen's step controls.
 *
 * Every step panel on that screen is replaced IN PLACE by the next one — the
 * next cow's outline step, the next question, the next flag row — with its
 * buttons in the same slots. So the second click of a double-click, which
 * arrives a few milliseconds after the first has already moved the screen on,
 * lands on the NEXT cow's button in the same place: a verdict or an answer for
 * a cow nobody has looked at. The browser numbers the clicks of a multi-click
 * in `event.detail` (1, 2, 3 ...); keyboard activation reports 0. */

/** `handler`, run for a single click (or a keyboard activation) and ignored
    for the second and later clicks of a multi-click. */
export function oneClick(handler: () => void): (e: { detail: number }) => void {
  return (e) => {
    if (e.detail > 1) return;
    handler();
  };
}
