import * as THREE from "three";

const KEYWORD = /^(from|import|class|def|async def|return)\b/;

export interface TextAtlas {
  texture: THREE.CanvasTexture;
  /** Per entry: u offset, v offset, u width (all in 0..1 texture space). */
  rects: Float32Array;
  /** Per entry: text width / row height, so planes keep the text's aspect. */
  aspects: Float32Array;
  rowAspect: number;
}

/**
 * One canvas texture for every code fragment, drawn in two columns, so the
 * whole code field is a single instanced draw call. Keywords are tinted;
 * everything else is drawn as plain source text.
 */
export function buildTextAtlas(lines: string[], fontFamily: string): TextAtlas {
  const size = 2048;
  const columns = 2;
  const rowHeight = 40;
  const fontSize = 25;
  const perColumn = Math.floor(size / rowHeight);
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const context = canvas.getContext("2d");
  const rects = new Float32Array(lines.length * 3);
  const aspects = new Float32Array(lines.length);
  const columnWidth = size / columns;

  if (context) {
    context.font = `${fontSize}px ${fontFamily}`;
    context.textBaseline = "middle";
    lines.slice(0, perColumn * columns).forEach((line, index) => {
      const column = Math.floor(index / perColumn);
      const row = index % perColumn;
      const x = column * columnWidth + 8;
      const y = row * rowHeight + rowHeight / 2;
      const keyword = line.match(KEYWORD)?.[0] ?? "";
      context.fillStyle = "#6cd0c2";
      context.fillText(keyword, x, y);
      const keywordWidth = context.measureText(keyword).width;
      context.fillStyle = "#c9d1d8";
      context.fillText(line.slice(keyword.length), x + keywordWidth, y);
      const width = Math.min(
        columnWidth - 16,
        context.measureText(line).width + 16,
      );
      rects[index * 3] = (column * columnWidth) / size;
      rects[index * 3 + 1] = 1 - ((row + 1) * rowHeight) / size;
      rects[index * 3 + 2] = width / size;
      aspects[index] = width / rowHeight;
    });
  }

  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  texture.needsUpdate = true;
  return { texture, rects, aspects, rowAspect: rowHeight / size };
}
