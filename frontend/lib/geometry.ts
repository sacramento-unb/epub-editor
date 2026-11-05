// Transformações entre o espaço do documento ("page_display_pt": pontos, origem no topo-esquerdo
// da CropBox já com a rotação intrínseca aplicada) e o viewport da interface (zoom + rotação extra,
// sentido horário, igual ao PDF.js).

export type BBox = [number, number, number, number];
export type Rotation = 0 | 90 | 180 | 270;

export function toView(b: BBox, pageW: number, pageH: number, scale: number, rot: Rotation): BBox {
  const pts: [number, number][] = [
    [b[0], b[1]],
    [b[2], b[3]],
  ].map(([x, y]) => point(x, y, pageW, pageH, rot));
  const xs = pts.map((p) => p[0] * scale);
  const ys = pts.map((p) => p[1] * scale);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

function point(x: number, y: number, w: number, h: number, rot: Rotation): [number, number] {
  switch (rot) {
    case 90:
      return [h - y, x];
    case 180:
      return [w - x, h - y];
    case 270:
      return [y, w - x];
    default:
      return [x, y];
  }
}

export function fromView(vx: number, vy: number, pageW: number, pageH: number, scale: number, rot: Rotation): [number, number] {
  const x = vx / scale;
  const y = vy / scale;
  switch (rot) {
    case 90:
      return [y, pageH - x];
    case 180:
      return [pageW - x, pageH - y];
    case 270:
      return [pageW - y, x];
    default:
      return [x, y];
  }
}

export function viewSize(pageW: number, pageH: number, scale: number, rot: Rotation): [number, number] {
  return rot === 90 || rot === 270 ? [pageH * scale, pageW * scale] : [pageW * scale, pageH * scale];
}

export function contains(b: BBox, x: number, y: number): boolean {
  return x >= b[0] && x <= b[2] && y >= b[1] && y <= b[3];
}

export function parsePageRanges(input: string, max: number): number[] {
  // "1-3, 5" -> [0,1,2,4] (índices 0-based), ignorando valores fora do documento.
  const out = new Set<number>();
  for (const part of input.split(",").map((s) => s.trim()).filter(Boolean)) {
    const m = part.match(/^(\d+)\s*(?:-\s*(\d+))?$/);
    if (!m) continue;
    const a = parseInt(m[1], 10);
    const b = m[2] ? parseInt(m[2], 10) : a;
    for (let i = Math.min(a, b); i <= Math.max(a, b); i++) if (i >= 1 && i <= max) out.add(i - 1);
  }
  return [...out].sort((x, y) => x - y);
}
