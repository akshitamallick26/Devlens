import { type GraphData, impactDepths } from "./data/graph";
import {
  AUTOMATION_SEQUENCE,
  QUALITY_PACKAGES,
  SCENES,
  SCENE_SIDES,
} from "./world/timeline";

/**
 * Reduced-motion and no-WebGL rendering of the same real graph: one 2D
 * canvas, redrawn only when the scene changes (never on a timer), with a
 * fixed projection. Emphasis per scene matches the WebGL world.
 */
export interface StaticGraph {
  draw: (scene: number) => void;
  resize: () => void;
  dispose: () => void;
}

export function createStaticGraph(
  canvas: HTMLCanvasElement,
  data: GraphData,
): StaticGraph {
  const context = canvas.getContext("2d");
  const depths = impactDepths(data);
  const count = data.names.length;
  const projected = new Float32Array(count * 2);
  let current = 0;

  const project = (sceneIndex: number) => {
    const narrow = window.innerWidth <= 720;
    const side = narrow ? 0 : (SCENE_SIDES[sceneIndex] ?? 0);
    // Fixed, slightly tilted view of the 3D layout, placed like the WebGL world.
    const scale = Math.min(canvas.width, canvas.height) / (narrow ? 30 : 36);
    const cx = canvas.width * (0.5 + side);
    const cy = canvas.height * (narrow ? 0.7 : 0.52);
    for (let i = 0; i < count; i++) {
      const x = data.positions[i * 3];
      const y = data.positions[i * 3 + 1];
      const z = data.positions[i * 3 + 2];
      projected[i * 2] = cx + (x + z * 0.32) * scale;
      projected[i * 2 + 1] = cy - (y * 0.9 - z * 0.18) * scale;
    }
  };

  const resize = () => {
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(window.innerWidth * ratio);
    canvas.height = Math.round(window.innerHeight * ratio);
    draw(current);
  };

  const emphasisFor = (scene: string) => {
    const name = (i: number) => data.packages[data.nodePackage[i]];
    if (scene === "impact" || scene === "intelligence")
      return (i: number) => depths[i] >= 0;
    if (scene === "quality")
      return (i: number) => QUALITY_PACKAGES.includes(name(i));
    if (scene === "automation")
      return (i: number) => AUTOMATION_SEQUENCE.includes(name(i));
    return null;
  };

  function draw(sceneIndex: number) {
    current = sceneIndex;
    if (!context) return;
    project(sceneIndex);
    const scene = SCENES[Math.min(Math.max(sceneIndex, 0), SCENES.length - 1)];
    const emphasis = emphasisFor(scene);
    const impactScene = scene === "impact" || scene === "intelligence";
    const ratio = canvas.width / Math.max(window.innerWidth, 1);
    const graphAlpha = scene === "intro" || scene === "code" ? 0.35 : 1;
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.lineWidth = Math.max(1, ratio * 0.6);

    for (let e = 0; e < data.edges.length; e += 3) {
      const s = data.edges[e];
      const t = data.edges[e + 1];
      const lit = emphasis !== null && emphasis(s) && emphasis(t);
      const onPath =
        impactScene &&
        depths[s] >= 0 &&
        depths[t] >= 0 &&
        depths[s] === depths[t] + 1;
      context.strokeStyle = onPath
        ? "rgba(245, 168, 87, 0.75)"
        : lit
          ? "rgba(128, 230, 214, 0.35)"
          : `rgba(140, 168, 184, ${(emphasis ? 0.04 : 0.09) * graphAlpha})`;
      context.beginPath();
      context.moveTo(projected[s * 2], projected[s * 2 + 1]);
      context.lineTo(projected[t * 2], projected[t * 2 + 1]);
      context.stroke();
    }
    for (let i = 0; i < count; i++) {
      const lit = emphasis !== null && emphasis(i);
      const radius =
        (data.kinds[data.nodeKind[i]] === "module" ? 2.2 : 1.5) *
        ratio *
        (lit ? 1.5 : 1);
      context.fillStyle = lit
        ? impactScene
          ? depths[i] === 0
            ? "rgba(255, 224, 160, 1)"
            : "rgba(245, 168, 87, 0.95)"
          : "rgba(128, 230, 214, 0.95)"
        : `rgba(170, 196, 210, ${(emphasis ? 0.18 : 0.55) * graphAlpha})`;
      context.beginPath();
      context.arc(
        projected[i * 2],
        projected[i * 2 + 1],
        radius,
        0,
        Math.PI * 2,
      );
      context.fill();
    }
  }

  window.addEventListener("resize", resize);
  resize();
  return {
    draw,
    resize,
    dispose: () => window.removeEventListener("resize", resize),
  };
}
