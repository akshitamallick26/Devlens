/**
 * The landing narrative as one continuous timeline. `t` runs from 0 (top of
 * the intro) to SCENES.length (end of the finale); each integer is the start
 * of the scene with that index. Every visual parameter is a pure function of
 * `t`, so scrolling backwards replays the story exactly in reverse.
 */
export const SCENES = [
  "intro",
  "code",
  "structure",
  "world",
  "impact",
  "intelligence",
  "quality",
  "automation",
  "architecture",
  "finale",
] as const;

export type SceneName = (typeof SCENES)[number];

export interface SceneState {
  /** Ambient coordinate field (intro and finale). */
  fieldOpacity: number;
  /** Floating source fragments. */
  codeOpacity: number;
  /** 0: fragments in their tunnel, 1: collapsed onto their module node. */
  morph: number;
  graphOpacity: number;
  /** Fraction of edges drawn (structure scene grows the graph). */
  edgeReveal: number;
  packageLabelOpacity: number;
  /** How strongly the impact view is applied. */
  impactMix: number;
  /** Reverse-traversal front, in hops from the seed. */
  impactFront: number;
  /** How strongly per-package emphasis (quality, automation) is applied. */
  packageMix: number;
  seedLabelOpacity: number;
}

const clamp01 = (value: number) => Math.min(1, Math.max(0, value));

/** Smoothstep between a and b. */
export function ramp(t: number, a: number, b: number): number {
  const x = clamp01((t - a) / (b - a));
  return x * x * (3 - 2 * x);
}

export function sceneState(
  t: number,
  maxImpactDepth: number,
  out: SceneState,
): SceneState {
  out.fieldOpacity = 1 - ramp(t, 0.6, 1.6) * 0.75 + ramp(t, 9.0, 9.8) * 0.6;
  out.codeOpacity = ramp(t, 0.7, 1.25) * (1 - ramp(t, 2.55, 2.95));
  out.morph = ramp(t, 2.0, 2.85);
  out.graphOpacity = ramp(t, 2.15, 2.9) * (1 - 0.5 * ramp(t, 9.15, 9.9));
  out.edgeReveal = ramp(t, 2.35, 3.4);
  out.packageLabelOpacity = ramp(t, 3.15, 3.45) * (1 - ramp(t, 3.8, 4.05));
  out.impactMix = ramp(t, 3.95, 4.2) * (1 - ramp(t, 5.85, 6.1));
  out.impactFront = ramp(t, 4.15, 4.8) * (maxImpactDepth + 1);
  out.packageMix = ramp(t, 6.0, 6.2) * (1 - ramp(t, 7.85, 8.1));
  out.seedLabelOpacity = ramp(t, 4.1, 4.3) * (1 - ramp(t, 5.85, 6.0));
  return out;
}

/**
 * Horizontal placement of the world per scene, as a fraction of the
 * viewport width: positive moves it right, away from text on the left.
 * Mirrors which side each scene's text column sits on.
 */
export const SCENE_SIDES = [
  0, 0.16, -0.18, 0.2, -0.22, -0.24, 0.24, -0.2, 0.25, 0,
];

export function emptySceneState(): SceneState {
  return {
    fieldOpacity: 1,
    codeOpacity: 0,
    morph: 0,
    graphOpacity: 0,
    edgeReveal: 0,
    packageLabelOpacity: 0,
    impactMix: 0,
    impactFront: 0,
    packageMix: 0,
    seedLabelOpacity: 0,
  };
}

/** Packages emphasized by the quality scene: the real testing and security subsystems. */
export const QUALITY_PACKAGES = ["testing", "security"];

/**
 * Automation's real stage order (app/automation/service.py): it drives
 * parsing, graph construction, impact reuse, the RAG rebuild, then testing
 * and security invalidation decisions.
 */
export const AUTOMATION_SEQUENCE = [
  "automation",
  "parsing",
  "graph",
  "impact",
  "rag",
  "testing",
  "security",
];

/** Per-package emphasis for scene position `t`, written into `out` (by package index). */
export function packageWeights(
  t: number,
  packages: readonly string[],
  out: Float32Array,
): Float32Array {
  out.fill(0);
  if (t >= 5.9 && t < 7.1) {
    for (const name of QUALITY_PACKAGES) {
      const index = packages.indexOf(name);
      if (index >= 0) out[index] = 1;
    }
  }
  if (t >= 6.9) {
    const local = clamp01((t - 7.05) / 0.8) * AUTOMATION_SEQUENCE.length;
    AUTOMATION_SEQUENCE.forEach((name, order) => {
      const index = packages.indexOf(name);
      if (index < 0) return;
      const lit = clamp01(local - order + 1);
      out[index] = Math.max(out[index], lit);
    });
  }
  return out;
}
