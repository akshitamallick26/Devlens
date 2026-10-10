import * as THREE from "three";
import { type GraphData, impactDepths, moduleNodeForFile } from "../data/graph";
import { separateLabels } from "./labelLayout";
import { buildTextAtlas } from "./textAtlas";
import {
  SCENES,
  SCENE_SIDES,
  emptySceneState,
  packageWeights,
  ramp,
  sceneState,
} from "./timeline";

export interface HoverInfo {
  name: string;
  packageName: string;
  kind: string;
}

export interface WorldOptions {
  /** Phones and coarse pointers: fewer fragments, lower resolution, no hover. */
  lite: boolean;
  fontFamily: string;
  /** Current scene position (see timeline.ts), read once per frame. */
  getProgress: () => number;
  /** Called when the hovered node changes (null: none). */
  onHover: (info: HoverInfo | null, clientX: number, clientY: number) => void;
  /** Called while a node stays hovered and the pointer moves. */
  onHoverMove: (clientX: number, clientY: number) => void;
  onContextLost: () => void;
}

export interface World {
  dispose: () => void;
}

const MAX_PACKAGES = 24;

const NODE_VERTEX = /* glsl */ `
  attribute float aPackage;
  attribute float aDepth;
  attribute float aSize;
  attribute float aIndex;
  attribute vec3 aColor;
  uniform float uPixelRatio;
  uniform float uOpacity;
  uniform float uImpactMix;
  uniform float uImpactFront;
  uniform float uPackageMix;
  uniform float uPackages[${MAX_PACKAGES}];
  uniform float uHover;
  uniform float uTime;
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    float impacted = aDepth >= 0.0 ? smoothstep(aDepth - 0.1, aDepth + 0.45, uImpactFront) : 0.0;
    float pkg = uPackages[int(aPackage + 0.5)];
    float emphasis = max(impacted * uImpactMix, pkg * uPackageMix);
    float dim = 1.0 - 0.78 * max(uImpactMix, uPackageMix);
    float brightness = mix(dim, 1.0, emphasis);
    vec3 seedColor = vec3(1.0, 0.88, 0.62);
    vec3 impactColor = mix(vec3(0.96, 0.64, 0.32), seedColor, aDepth == 0.0 ? 1.0 : 0.0);
    vec3 color = mix(aColor, impactColor, impacted * uImpactMix);
    color = mix(color, vec3(0.55, 0.93, 0.86), pkg * uPackageMix * 0.7);
    float hovered = abs(aIndex - uHover) < 0.5 ? 1.0 : 0.0;
    vColor = color * brightness + hovered * 0.35;
    float fade = smoothstep(95.0, 30.0, -mv.z);
    vAlpha = uOpacity * mix(0.3, 1.0, brightness) * fade;
    float pulse = aDepth == 0.0 ? uImpactMix * (2.5 + 1.2 * sin(uTime * 2.6)) : 0.0;
    float size = aSize * (1.0 + emphasis * 0.9) + pulse + hovered * 2.5;
    gl_PointSize = size * uPixelRatio * (34.0 / max(-mv.z, 1.0));
    gl_Position = projectionMatrix * mv;
  }
`;

const NODE_FRAGMENT = /* glsl */ `
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    float d = length(gl_PointCoord - 0.5);
    float halo = smoothstep(0.5, 0.0, d);
    float core = smoothstep(0.2, 0.0, d);
    float alpha = (halo * 0.55 + core * 0.75) * vAlpha;
    if (alpha < 0.01) discard;
    gl_FragColor = vec4(vColor * (0.55 + core), alpha);
  }
`;

const EDGE_VERTEX = /* glsl */ `
  attribute float aOrder;
  attribute float aImpact;
  attribute float aPackage;
  uniform float uReveal;
  uniform float uOpacity;
  uniform float uImpactMix;
  uniform float uImpactFront;
  uniform float uPackageMix;
  uniform float uPackages[${MAX_PACKAGES}];
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    float shown = step(aOrder, uReveal);
    float lit = aImpact >= 0.0 ? smoothstep(aImpact - 0.15, aImpact + 0.5, uImpactFront) : 0.0;
    float pkg = uPackages[int(aPackage + 0.5)];
    float emphasis = max(lit * uImpactMix, pkg * uPackageMix);
    float base = 0.11 * (1.0 - 0.75 * max(uImpactMix, uPackageMix));
    vColor = mix(vec3(0.55, 0.66, 0.72), vec3(0.98, 0.66, 0.34), lit * uImpactMix);
    vColor = mix(vColor, vec3(0.5, 0.9, 0.84), pkg * uPackageMix * (1.0 - lit));
    float fade = smoothstep(95.0, 30.0, -mv.z);
    vAlpha = uOpacity * shown * fade * mix(base, 0.85, emphasis);
    gl_Position = projectionMatrix * mv;
  }
`;

const EDGE_FRAGMENT = /* glsl */ `
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    if (vAlpha < 0.004) discard;
    gl_FragColor = vec4(vColor, vAlpha);
  }
`;

const CODE_VERTEX = /* glsl */ `
  attribute vec3 aStart;
  attribute vec3 aTarget;
  attribute vec3 aRect;
  attribute float aAspect;
  attribute float aSeed;
  uniform float uMorph;
  uniform float uRowAspect;
  uniform float uScale;
  uniform float uTime;
  varying vec2 vUv;
  varying float vAlpha;
  void main() {
    float m = smoothstep(0.0, 1.0, clamp(uMorph * 1.5 - aSeed * 0.5, 0.0, 1.0));
    vec3 drift = vec3(sin(uTime * 0.21 + aSeed * 6.0), cos(uTime * 0.17 + aSeed * 4.0), 0.0) * 0.35;
    vec3 center = mix(aStart + drift, aTarget, m);
    vec3 right = vec3(viewMatrix[0][0], viewMatrix[1][0], viewMatrix[2][0]);
    vec3 up = vec3(viewMatrix[0][1], viewMatrix[1][1], viewMatrix[2][1]);
    float scale = uScale * (1.0 - m * 0.97);
    vec3 world = center + (right * position.x * aAspect + up * position.y) * scale;
    vec4 mv = viewMatrix * vec4(world, 1.0);
    vUv = vec2(aRect.x + (position.x + 0.5) * aRect.z, aRect.y + (position.y + 0.5) * uRowAspect);
    float near = smoothstep(7.0, 16.0, -mv.z);
    float far = smoothstep(80.0, 40.0, -mv.z);
    vAlpha = near * far * (1.0 - m * 0.85);
    gl_Position = projectionMatrix * mv;
  }
`;

const CODE_FRAGMENT = /* glsl */ `
  uniform sampler2D uAtlas;
  uniform float uOpacity;
  varying vec2 vUv;
  varying float vAlpha;
  void main() {
    vec4 texel = texture2D(uAtlas, vUv);
    float alpha = texel.a * uOpacity * vAlpha;
    if (alpha < 0.01) discard;
    gl_FragColor = vec4(texel.rgb, alpha);
  }
`;

const FIELD_VERTEX = /* glsl */ `
  uniform float uTime;
  uniform float uOpacity;
  uniform vec3 uPointer;
  uniform float uPixelRatio;
  varying float vAlpha;
  void main() {
    vec3 p = position;
    float wave = sin(p.x * 0.09 + uTime * 0.35) * cos(p.z * 0.07 + uTime * 0.25);
    float d = distance(p.xz, uPointer.xz);
    float ripple = exp(-d * 0.09) * sin(d * 0.6 - uTime * 2.2) * uPointer.y;
    p.y += wave * 0.9 + ripple * 1.6;
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    float fade = smoothstep(150.0, 20.0, -mv.z);
    vAlpha = uOpacity * fade * (0.4 + 0.55 * max(ripple, 0.0));
    gl_PointSize = uPixelRatio * (58.0 / max(-mv.z, 1.0));
    gl_Position = projectionMatrix * mv;
  }
`;

const FIELD_FRAGMENT = /* glsl */ `
  varying float vAlpha;
  void main() {
    float d = length(gl_PointCoord - 0.5);
    float alpha = smoothstep(0.5, 0.1, d) * vAlpha;
    if (alpha < 0.01) discard;
    gl_FragColor = vec4(0.52, 0.72, 0.74, alpha);
  }
`;

/** Deterministic pseudo-random in [0, 1). */
function hash(n: number): number {
  const x = Math.sin(n * 12.9898 + 78.233) * 43758.5453;
  return x - Math.floor(x);
}

function labelSprite(text: string, fontFamily: string, accent: boolean) {
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  const fontSize = 30;
  const font = `${fontSize}px ${fontFamily}`;
  let width = 256;
  if (context) {
    context.font = font;
    width = Math.ceil(context.measureText(text).width) + 24;
  }
  canvas.width = width;
  canvas.height = 48;
  if (context) {
    context.font = font;
    context.textBaseline = "middle";
    context.fillStyle = accent ? "#ffd99e" : "#9fb4c2";
    context.fillText(text, 12, 24);
  }
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const material = new THREE.SpriteMaterial({
    map: texture,
    transparent: true,
    depthWrite: false,
    depthTest: false,
    sizeAttenuation: false,
    opacity: 0,
  });
  const sprite = new THREE.Sprite(material);
  const height = 0.032;
  sprite.scale.set((height * width) / 48, height, 1);
  sprite.center.set(0, 0.5);
  return sprite;
}

export function createWorld(
  canvas: HTMLCanvasElement,
  data: GraphData,
  options: WorldOptions,
): World {
  const renderer = new THREE.WebGLRenderer({
    canvas,
    antialias: !options.lite,
    alpha: false,
    powerPreference: "high-performance",
  });
  const pixelRatio = Math.min(
    window.devicePixelRatio || 1,
    options.lite ? 1.25 : 1.75,
  );
  renderer.setPixelRatio(pixelRatio);
  renderer.setClearColor(0x07090b, 1);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 400);

  const nodeCount = data.names.length;
  const depths = impactDepths(data);
  const positions = new Float32Array(data.positions);
  const packages = data.packages;

  // ---------------- dependency world: nodes ----------------
  const nodeGeometry = new THREE.BufferGeometry();
  nodeGeometry.setAttribute(
    "position",
    new THREE.BufferAttribute(positions, 3),
  );
  const nodePackage = new Float32Array(data.nodePackage);
  const nodeDepth = Float32Array.from(depths);
  const nodeSize = new Float32Array(nodeCount);
  const nodeIndex = new Float32Array(nodeCount);
  const nodeColor = new Float32Array(nodeCount * 3);
  for (let i = 0; i < nodeCount; i++) {
    const degree = data.nodeDegree[i];
    const kind = data.kinds[data.nodeKind[i]];
    nodeSize[i] =
      (kind === "module" ? 3.2 : kind === "class" ? 2.6 : 2.0) +
      Math.min(degree, 30) * 0.08;
    nodeIndex[i] = i;
    // Restrained, near-neutral tint per package: clusters read apart
    // without turning the graph into a palette.
    const tint = hash(data.nodePackage[i] + 3);
    nodeColor[i * 3] = 0.55 + tint * 0.12;
    nodeColor[i * 3 + 1] = 0.68 + tint * 0.1;
    nodeColor[i * 3 + 2] = 0.74 + (1 - tint) * 0.08;
  }
  nodeGeometry.setAttribute(
    "aPackage",
    new THREE.BufferAttribute(nodePackage, 1),
  );
  nodeGeometry.setAttribute("aDepth", new THREE.BufferAttribute(nodeDepth, 1));
  nodeGeometry.setAttribute("aSize", new THREE.BufferAttribute(nodeSize, 1));
  nodeGeometry.setAttribute("aIndex", new THREE.BufferAttribute(nodeIndex, 1));
  nodeGeometry.setAttribute("aColor", new THREE.BufferAttribute(nodeColor, 3));
  const packageUniform = { value: new Array<number>(MAX_PACKAGES).fill(0) };
  const sharedUniforms = {
    uImpactMix: { value: 0 },
    uImpactFront: { value: 0 },
    uPackageMix: { value: 0 },
    uPackages: packageUniform,
  };
  const nodeMaterial = new THREE.ShaderMaterial({
    vertexShader: NODE_VERTEX,
    fragmentShader: NODE_FRAGMENT,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    uniforms: {
      ...sharedUniforms,
      uPixelRatio: { value: pixelRatio },
      uOpacity: { value: 0 },
      uHover: { value: -1 },
      uTime: { value: 0 },
    },
  });
  const nodes = new THREE.Points(nodeGeometry, nodeMaterial);
  nodes.frustumCulled = false;
  scene.add(nodes);

  // ---------------- dependency world: edges ----------------
  const edgeCount = data.edges.length / 3;
  const edgePositions = new Float32Array(edgeCount * 6);
  const edgeOrder = new Float32Array(edgeCount * 2);
  const edgeImpact = new Float32Array(edgeCount * 2);
  const edgePackage = new Float32Array(edgeCount * 2);
  for (let e = 0; e < edgeCount; e++) {
    const source = data.edges[e * 3];
    const target = data.edges[e * 3 + 1];
    for (let k = 0; k < 3; k++) {
      edgePositions[e * 6 + k] = positions[source * 3 + k];
      edgePositions[e * 6 + 3 + k] = positions[target * 3 + k];
    }
    // Structure grows from the busiest regions outward.
    const order =
      hash(e + 11) * 0.75 +
      (1 - Math.min(data.nodeDegree[source], 20) / 20) * 0.25;
    // source = dependent, target = dependency: the reverse traversal walks
    // from target to source, so an edge is on the impact path when the
    // dependent sits exactly one hop further from the seed.
    const onPath =
      depths[source] >= 0 &&
      depths[target] >= 0 &&
      depths[source] === depths[target] + 1;
    const impact = onPath ? depths[source] : -1;
    edgeOrder[e * 2] = edgeOrder[e * 2 + 1] = order;
    edgeImpact[e * 2] = edgeImpact[e * 2 + 1] = impact;
    edgePackage[e * 2] = data.nodePackage[source];
    edgePackage[e * 2 + 1] = data.nodePackage[target];
  }
  const edgeGeometry = new THREE.BufferGeometry();
  edgeGeometry.setAttribute(
    "position",
    new THREE.BufferAttribute(edgePositions, 3),
  );
  edgeGeometry.setAttribute("aOrder", new THREE.BufferAttribute(edgeOrder, 1));
  edgeGeometry.setAttribute(
    "aImpact",
    new THREE.BufferAttribute(edgeImpact, 1),
  );
  edgeGeometry.setAttribute(
    "aPackage",
    new THREE.BufferAttribute(edgePackage, 1),
  );
  const edgeMaterial = new THREE.ShaderMaterial({
    vertexShader: EDGE_VERTEX,
    fragmentShader: EDGE_FRAGMENT,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    uniforms: {
      ...sharedUniforms,
      uReveal: { value: 0 },
      uOpacity: { value: 0 },
    },
  });
  const edges = new THREE.LineSegments(edgeGeometry, edgeMaterial);
  edges.frustumCulled = false;
  scene.add(edges);

  // ---------------- code field: real fragments, one instanced draw ----------------
  const fragments = data.codeFragments.slice(
    0,
    options.lite ? 44 : data.codeFragments.length,
  );
  const atlas = buildTextAtlas(
    fragments.map((f) => f.text),
    options.fontFamily,
  );
  const plane = new THREE.PlaneGeometry(1, 1);
  const codeGeometry = new THREE.InstancedBufferGeometry();
  codeGeometry.index = plane.index;
  codeGeometry.setAttribute("position", plane.getAttribute("position"));
  codeGeometry.instanceCount = fragments.length;
  const start = new Float32Array(fragments.length * 3);
  const target = new Float32Array(fragments.length * 3);
  const seeds = new Float32Array(fragments.length);
  fragments.forEach((fragment, i) => {
    const angle = hash(i + 1) * Math.PI * 2;
    const radius = 3.2 + hash(i + 2) * 7.5;
    // Most fragments drift on the side away from the text column; the
    // rest stay far back, small and faint behind it.
    const front = hash(i + 5) < 0.72;
    start[i * 3] = front
      ? 2 + Math.abs(Math.cos(angle)) * radius * 1.4
      : -Math.abs(Math.cos(angle)) * radius * 1.2;
    start[i * 3 + 1] = Math.sin(angle) * radius * 0.75;
    start[i * 3 + 2] = (front ? 34 : 18) + hash(i + 3) * 52;
    const node = moduleNodeForFile(data, fragment.file);
    const n = node >= 0 ? node : 0;
    target[i * 3] = positions[n * 3];
    target[i * 3 + 1] = positions[n * 3 + 1];
    target[i * 3 + 2] = positions[n * 3 + 2];
    seeds[i] = hash(i + 4);
  });
  codeGeometry.setAttribute(
    "aStart",
    new THREE.InstancedBufferAttribute(start, 3),
  );
  codeGeometry.setAttribute(
    "aTarget",
    new THREE.InstancedBufferAttribute(target, 3),
  );
  codeGeometry.setAttribute(
    "aRect",
    new THREE.InstancedBufferAttribute(atlas.rects, 3),
  );
  codeGeometry.setAttribute(
    "aAspect",
    new THREE.InstancedBufferAttribute(atlas.aspects, 1),
  );
  codeGeometry.setAttribute(
    "aSeed",
    new THREE.InstancedBufferAttribute(seeds, 1),
  );
  const codeMaterial = new THREE.ShaderMaterial({
    vertexShader: CODE_VERTEX,
    fragmentShader: CODE_FRAGMENT,
    transparent: true,
    depthWrite: false,
    uniforms: {
      uAtlas: { value: atlas.texture },
      uMorph: { value: 0 },
      uRowAspect: { value: atlas.rowAspect },
      uScale: { value: options.lite ? 0.5 : 0.52 },
      uOpacity: { value: 0 },
      uTime: { value: 0 },
    },
  });
  const code = new THREE.Mesh(codeGeometry, codeMaterial);
  code.frustumCulled = false;
  scene.add(code);

  // ---------------- ambient coordinate field ----------------
  const columns = options.lite ? 44 : 96;
  const rows = options.lite ? 40 : 80;
  const fieldPositions = new Float32Array(columns * rows * 3);
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < columns; c++) {
      const i = (r * columns + c) * 3;
      fieldPositions[i] = (c / (columns - 1) - 0.5) * 170;
      fieldPositions[i + 1] = -12;
      fieldPositions[i + 2] = (r / (rows - 1)) * 180 - 70;
    }
  }
  const fieldGeometry = new THREE.BufferGeometry();
  fieldGeometry.setAttribute(
    "position",
    new THREE.BufferAttribute(fieldPositions, 3),
  );
  const pointerUniform = { value: new THREE.Vector3(0, 0, 60) };
  const fieldMaterial = new THREE.ShaderMaterial({
    vertexShader: FIELD_VERTEX,
    fragmentShader: FIELD_FRAGMENT,
    transparent: true,
    depthWrite: false,
    uniforms: {
      uTime: { value: 0 },
      uOpacity: { value: 1 },
      uPointer: pointerUniform,
      uPixelRatio: { value: pixelRatio },
    },
  });
  const field = new THREE.Points(fieldGeometry, fieldMaterial);
  field.frustumCulled = false;
  scene.add(field);

  // ---------------- labels: package clusters and the impact seed ----------------
  const sizeByPackage = new Map<number, number>();
  data.nodePackage.forEach((p) =>
    sizeByPackage.set(p, (sizeByPackage.get(p) ?? 0) + 1),
  );
  const packageLabels: THREE.Sprite[] = [];
  packages.forEach((name, index) => {
    if ((sizeByPackage.get(index) ?? 0) < 18 || name === "app") return;
    const centroid = (data.packageCentroids as Record<string, number[]>)[name];
    const sprite = labelSprite(name, options.fontFamily, false);
    sprite.position.set(centroid[0], centroid[1] + 2.4, centroid[2]);
    sprite.userData.anchor = sprite.position.clone();
    packageLabels.push(sprite);
    scene.add(sprite);
  });
  const seed = data.impact.seed;
  const seedLabel = labelSprite(
    data.names[seed].split(".").pop() ?? "",
    options.fontFamily,
    true,
  );
  seedLabel.position.set(
    positions[seed * 3] + 0.8,
    positions[seed * 3 + 1] + 0.8,
    positions[seed * 3 + 2],
  );
  scene.add(seedLabel);

  // ---------------- camera path ----------------
  const centroidOf = (indices: number[]) => {
    const v = new THREE.Vector3();
    indices.forEach((i) =>
      v.add(
        new THREE.Vector3(
          positions[i * 3],
          positions[i * 3 + 1],
          positions[i * 3 + 2],
        ),
      ),
    );
    return v.multiplyScalar(1 / Math.max(indices.length, 1));
  };
  const impacted: number[] = [];
  depths.forEach((d, i) => d >= 0 && impacted.push(i));
  const quality: number[] = [];
  data.nodePackage.forEach((p, i) => {
    if (packages[p] === "testing" || packages[p] === "security")
      quality.push(i);
  });
  const impactCenter = centroidOf(impacted);
  const seedPosition = centroidOf([seed]);
  const qualityCenter = centroidOf(quality);
  const zero = new THREE.Vector3();
  // One key per scene boundary: [camera position, look-at target].
  const keys: [THREE.Vector3, THREE.Vector3][] = [
    [new THREE.Vector3(0, 2, 102), new THREE.Vector3(0, 0, 60)],
    [new THREE.Vector3(0, 1, 90), new THREE.Vector3(0, 0, 40)],
    [new THREE.Vector3(0, 2, 48), new THREE.Vector3(0, 0, 0)],
    [new THREE.Vector3(5, 7, 33), zero.clone()],
    [new THREE.Vector3(-12, 9, 27), zero.clone()],
    [
      seedPosition
        .clone()
        .lerp(impactCenter, 0.5)
        .add(new THREE.Vector3(6, 5, 15)),
      seedPosition.clone().lerp(impactCenter, 0.5),
    ],
    [
      impactCenter.clone().add(new THREE.Vector3(-3, 3, 14)),
      impactCenter.clone().add(new THREE.Vector3(4, 0, 0)),
    ],
    [
      qualityCenter.clone().add(new THREE.Vector3(6, 6, 18)),
      qualityCenter.clone(),
    ],
    [new THREE.Vector3(16, 10, 26), zero.clone()],
    [new THREE.Vector3(0, 24, 30), zero.clone()],
    [new THREE.Vector3(0, 9, 66), zero.clone()],
  ];

  /** The camera path at scene position t (before pointer parallax). */
  const poseAt = (
    t: number,
    outPosition: THREE.Vector3,
    outTarget: THREE.Vector3,
  ) => {
    const index = Math.min(Math.floor(t), keys.length - 2);
    const blend = ramp(t - index, 0, 1);
    outPosition.lerpVectors(keys[index][0], keys[index + 1][0], blend);
    outTarget.lerpVectors(keys[index][1], keys[index + 1][1], blend);
    // A slow orbit in the dependency-world scenes keeps depth legible.
    const orbit =
      ramp(t, 3, 4) * (1 - ramp(t, 4, 4.5)) * 0.35 + ramp(t, 7, 8.4) * 0.25;
    outPosition
      .sub(outTarget)
      .applyAxisAngle(THREE.Object3D.DEFAULT_UP, orbit)
      .add(outTarget);
    return index;
  };

  /**
   * Package labels keep a constant screen size, so two packages whose
   * centroids project close together collide. Once per viewport size (never
   * per frame), project the labels from the camera poses the world scene
   * passes through while they are visible and nudge colliding ones apart
   * vertically. Positions start from each label's anchor every time.
   */
  // Scene positions sampled across the window in which package labels show
  // (timeline.ts: packageLabelOpacity, about t = 3.15 to 4.05).
  const LABEL_POSES = [3.15, 3.3, 3.5, 3.7, 3.9, 4.05];
  const labelCamera = new THREE.PerspectiveCamera();
  const labelPosition = new THREE.Vector3();
  const labelTarget = new THREE.Vector3();
  const projectedLabel = new THREE.Vector3();
  const layoutPackageLabels = (width: number, height: number) => {
    for (const label of packageLabels)
      label.position.copy(label.userData.anchor);
    labelCamera.fov = camera.fov;
    labelCamera.aspect = width / height;
    labelCamera.updateProjectionMatrix();
    const focal = 1 / Math.tan(THREE.MathUtils.degToRad(labelCamera.fov) / 2);
    // Repeat over every sampled pose until a full round needs no shift: a
    // nudge that clears one pose can otherwise create an overlap at another.
    // Shifts only move labels down, so this settles within a few rounds.
    for (let round = 0; round < 8; round++) {
      let moved = false;
      for (const t of LABEL_POSES) {
        poseAt(t, labelPosition, labelTarget);
        labelCamera.position.copy(labelPosition);
        labelCamera.lookAt(labelTarget);
        labelCamera.updateMatrixWorld();
        const depths: number[] = [];
        const rects = packageLabels.map((label) => {
          projectedLabel
            .copy(label.position)
            .applyMatrix4(labelCamera.matrixWorldInverse);
          depths.push(-projectedLabel.z);
          projectedLabel.copy(label.position).project(labelCamera);
          const labelHeight = (label.scale.y * focal * height) / 2;
          return {
            x: ((projectedLabel.x + 1) / 2) * width,
            y: ((1 - projectedLabel.y) / 2) * height - labelHeight / 2,
            width: (label.scale.x * focal * height) / 2,
            height: labelHeight,
          };
        });
        separateLabels(rects).forEach((shift, i) => {
          if (shift < 0.5) return;
          moved = true;
          const worldPerPixel = (2 * depths[i]) / (focal * height);
          packageLabels[i].position.y -= shift * worldPerPixel;
        });
      }
      if (!moved) break;
    }
  };

  // ---------------- interaction ----------------
  const raycaster = new THREE.Raycaster();
  raycaster.params.Points = { threshold: 0.45 };
  const ndc = new THREE.Vector2();
  const parallax = new THREE.Vector2();
  const parallaxTarget = new THREE.Vector2();
  let pointerClient = { x: 0, y: 0 };
  let pointerDirty = false;
  let hovered = -1;
  const groundPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 12);
  const groundHit = new THREE.Vector3();

  const onPointerMove = (event: PointerEvent) => {
    if (event.pointerType !== "mouse") return;
    pointerClient = { x: event.clientX, y: event.clientY };
    ndc.set(
      (event.clientX / window.innerWidth) * 2 - 1,
      -(event.clientY / window.innerHeight) * 2 + 1,
    );
    parallaxTarget.set(ndc.x, ndc.y);
    pointerDirty = true;
  };
  if (!options.lite)
    window.addEventListener("pointermove", onPointerMove, { passive: true });

  // ---------------- frame loop ----------------
  const state = emptySceneState();
  const weights = new Float32Array(MAX_PACKAGES);
  const cameraPosition = keys[0][0].clone();
  const cameraTarget = keys[0][1].clone();
  const desiredPosition = new THREE.Vector3();
  const desiredTarget = new THREE.Vector3();
  const maxDepth = data.provenance.impactMaxDepth;
  let smoothT = options.getProgress();
  let frame = 0;
  let last = performance.now();
  let running = false;
  let viewShift = 0;
  const viewport = { width: window.innerWidth, height: window.innerHeight };

  const resize = () => {
    const width = window.innerWidth;
    const height = window.innerHeight;
    viewport.width = width;
    viewport.height = height;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    // Narrow screens: pull back so the graph still fits the width.
    camera.fov = camera.aspect < 0.8 ? 72 : 55;
    if (options.lite) {
      // Phones: text fills the top of the screen, the world sits below it.
      camera.setViewOffset(width, height, 0, -0.2 * height, width, height);
    }
    camera.updateProjectionMatrix();
    if (!options.lite) layoutPackageLabels(width, height);
  };
  resize();

  const tick = (now: number) => {
    frame = requestAnimationFrame(tick);
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    const time = now / 1000;

    smoothT += (options.getProgress() - smoothT) * (1 - Math.exp(-dt * 4));
    const t = Math.min(Math.max(smoothT, 0), SCENES.length);
    sceneState(t, maxDepth, state);
    packageWeights(t, packages, weights);
    for (let i = 0; i < MAX_PACKAGES; i++) packageUniform.value[i] = weights[i];

    const index = poseAt(t, desiredPosition, desiredTarget);
    parallax.lerp(parallaxTarget, 1 - Math.exp(-dt * 2));
    desiredPosition.x += parallax.x * 1.4;
    desiredPosition.y += parallax.y * 0.9;
    const follow = 1 - Math.exp(-dt * 3);
    cameraPosition.lerp(desiredPosition, follow);
    cameraTarget.lerp(desiredTarget, follow);
    camera.position.copy(cameraPosition);
    camera.lookAt(cameraTarget);

    // Keep the world beside the scene's text column (desktop only).
    if (!options.lite) {
      const side =
        SCENE_SIDES[index] +
        (SCENE_SIDES[index + 1] - SCENE_SIDES[index]) *
          ramp(t - index, 0.55, 1);
      viewShift += (side - viewShift) * follow;
      camera.setViewOffset(
        viewport.width,
        viewport.height,
        -viewShift * viewport.width,
        0,
        viewport.width,
        viewport.height,
      );
    }

    nodeMaterial.uniforms.uOpacity.value = state.graphOpacity;
    nodeMaterial.uniforms.uTime.value = time;
    edgeMaterial.uniforms.uOpacity.value = state.graphOpacity;
    edgeMaterial.uniforms.uReveal.value = state.edgeReveal;
    sharedUniforms.uImpactMix.value = state.impactMix;
    sharedUniforms.uImpactFront.value = state.impactFront;
    sharedUniforms.uPackageMix.value = state.packageMix;
    codeMaterial.uniforms.uOpacity.value = state.codeOpacity;
    codeMaterial.uniforms.uMorph.value = state.morph;
    codeMaterial.uniforms.uTime.value = time;
    code.visible = state.codeOpacity > 0.005;
    fieldMaterial.uniforms.uOpacity.value = state.fieldOpacity;
    fieldMaterial.uniforms.uTime.value = time;
    field.visible = state.fieldOpacity > 0.01;
    for (const label of packageLabels)
      label.material.opacity = options.lite
        ? 0
        : state.packageLabelOpacity * 0.9;
    seedLabel.material.opacity = state.seedLabelOpacity;

    // Pointer: ground ripple and node hover (desktop, graph scenes only).
    pointerUniform.value.y *= Math.exp(-dt * 1.5);
    if (pointerDirty) {
      pointerDirty = false;
      raycaster.setFromCamera(ndc, camera);
      if (raycaster.ray.intersectPlane(groundPlane, groundHit)) {
        pointerUniform.value.set(groundHit.x, 1, groundHit.z);
      }
      const hoverable = state.graphOpacity > 0.6 && t > 3 && t < 6.2;
      let next = -1;
      if (hoverable) {
        const hits = raycaster.intersectObject(nodes, false);
        if (hits.length && hits[0].index !== undefined) next = hits[0].index;
      }
      if (next !== hovered) {
        hovered = next;
        nodeMaterial.uniforms.uHover.value = hovered;
        options.onHover(
          hovered >= 0
            ? {
                name: data.names[hovered],
                packageName: packages[data.nodePackage[hovered]],
                kind: data.kinds[data.nodeKind[hovered]],
              }
            : null,
          pointerClient.x,
          pointerClient.y,
        );
      } else if (hovered >= 0) {
        options.onHoverMove(pointerClient.x, pointerClient.y);
      }
    }

    renderer.render(scene, camera);
  };

  const startLoop = () => {
    if (running) return;
    running = true;
    last = performance.now();
    frame = requestAnimationFrame(tick);
  };
  const stopLoop = () => {
    running = false;
    cancelAnimationFrame(frame);
  };
  const onVisibility = () => (document.hidden ? stopLoop() : startLoop());
  const onContextLost = (event: Event) => {
    event.preventDefault();
    stopLoop();
    options.onContextLost();
  };

  window.addEventListener("resize", resize);
  document.addEventListener("visibilitychange", onVisibility);
  canvas.addEventListener("webglcontextlost", onContextLost);
  startLoop();

  return {
    dispose() {
      stopLoop();
      window.removeEventListener("resize", resize);
      window.removeEventListener("pointermove", onPointerMove);
      document.removeEventListener("visibilitychange", onVisibility);
      canvas.removeEventListener("webglcontextlost", onContextLost);
      scene.traverse((object) => {
        const mesh = object as THREE.Mesh;
        mesh.geometry?.dispose();
        const material = mesh.material as
          THREE.Material | THREE.Material[] | undefined;
        for (const m of Array.isArray(material)
          ? material
          : material
            ? [material]
            : []) {
          const map = (m as THREE.SpriteMaterial).map;
          map?.dispose();
          m.dispose();
        }
      });
      plane.dispose();
      atlas.texture.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
    },
  };
}
