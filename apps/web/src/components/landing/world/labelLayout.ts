/** A label's screen rectangle in CSS pixels; `y` is the top edge. */
export interface LabelRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * Deterministic label de-overlap: labels are visited top to bottom (then
 * left to right, then by index) and each is pushed down until it clears
 * every label already placed. Returns the downward shift in pixels for each
 * input rectangle; labels that already clear the others get 0.
 */
export function separateLabels(rects: LabelRect[], gap = 6): number[] {
  const order = rects
    .map((_, i) => i)
    .sort(
      (a, b) => rects[a].y - rects[b].y || rects[a].x - rects[b].x || a - b,
    );
  const placed: LabelRect[] = [];
  const shifts = new Array<number>(rects.length).fill(0);
  for (const i of order) {
    const rect = { ...rects[i] };
    let moved = true;
    while (moved) {
      moved = false;
      for (const other of placed) {
        const overlaps =
          rect.x < other.x + other.width + gap &&
          other.x < rect.x + rect.width + gap &&
          rect.y < other.y + other.height + gap &&
          other.y < rect.y + rect.height + gap;
        if (overlaps) {
          rect.y = other.y + other.height + gap;
          moved = true;
        }
      }
    }
    shifts[i] = rect.y - rects[i].y;
    placed.push(rect);
  }
  return shifts;
}
