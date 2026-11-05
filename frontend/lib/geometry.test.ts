import { test } from "node:test";
import assert from "node:assert/strict";
import { fromView, parsePageRanges, toView, viewSize, type BBox, type Rotation } from "./geometry.ts";

const W = 600, H = 800;
const box: BBox = [100, 50, 300, 90];

test("ida e volta em todos os zooms e rotações", () => {
  for (const scale of [0.5, 1, 1.75, 3]) {
    for (const rot of [0, 90, 180, 270] as Rotation[]) {
      const v = toView(box, W, H, scale, rot);
      const [vw, vh] = viewSize(W, H, scale, rot);
      assert.ok(v[0] >= 0 && v[2] <= vw + 1e-9 && v[1] >= 0 && v[3] <= vh + 1e-9, `${scale}/${rot} dentro da página`);
      const a = fromView(v[0], v[1], W, H, scale, rot);
      const b = fromView(v[2], v[3], W, H, scale, rot);
      const back = [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])];
      back.forEach((n, i) => assert.ok(Math.abs(n - box[i]) < 1e-9, `${scale}/${rot} coordenada ${i}`));
    }
  }
});

test("rotação de 90° no sentido horário (convenção do PDF.js)", () => {
  // canto superior esquerdo da página vai para o canto superior direito
  assert.deepEqual(toView([0, 0, 10, 10], W, H, 1, 90), [H - 10, 0, H, 10]);
  assert.deepEqual(viewSize(W, H, 2, 90), [H * 2, W * 2]);
});

test("intervalos de páginas", () => {
  assert.deepEqual(parsePageRanges("1-3, 5, 9-7, 99", 9), [0, 1, 2, 4, 6, 7, 8]);
  assert.deepEqual(parsePageRanges("x, 0", 3), []);
});
