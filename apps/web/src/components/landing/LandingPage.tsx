import Link from "next/link";
import { Fragment } from "react";
import { graphData } from "./data/graph";
import { LandingWorld } from "./LandingWorld";
import { RevealOnScroll } from "./RevealOnScroll";
import styles from "./landing.module.css";

const WORKSPACE = "/workspace";
const CONTENT_ID = "landing-content";

const number = (n: number) => n.toLocaleString("en-US");
const shortName = (qualified: string) => qualified.replace(/^app\./, "");

/** A qualified name that may wrap after any dot, never inside an identifier. */
function SymbolName({ name }: { name: string }) {
  const parts = shortName(name).split(".");
  return (
    <>
      {parts.map((part, i) => (
        <Fragment key={i}>
          {i > 0 ? "." : null}
          {i > 0 ? <wbr /> : null}
          {part}
        </Fragment>
      ))}
    </>
  );
}

const { provenance, impactedSymbols, codeFragments } = graphData;
const seedSymbol = impactedSymbols.find((s) => s.depth === 0)!;
const dependents = impactedSymbols.filter((s) => s.depth > 0);
const depthGroups = Array.from(
  { length: provenance.impactMaxDepth },
  (_, i) => ({
    depth: i + 1,
    symbols: dependents.filter((s) => s.depth === i + 1),
  }),
);
// Evidence shown in the intelligence scene: the seed and the first
// function-level dependents, with their real files and line spans.
const evidence = [
  seedSymbol,
  ...dependents.filter((s) => s.depth === 1 && s.name.split(".").length > 3),
].slice(0, 4);
const specimen = ["from ", "def ", "return "].map((prefix) =>
  codeFragments.find((f) => f.text.startsWith(prefix))!,
);

const ARCHITECTURE: [string, string][] = [
  [
    "Ingestion",
    "Clones a public GitHub repository at one commit and stores every file of that revision, content-addressed.",
  ],
  [
    "Parsing",
    "Python AST into revision-scoped symbols: modules, classes, functions and methods, with ids that stay stable across re-parses.",
  ],
  [
    "Graph",
    "Calls, imports and inheritance as directed edges from dependent to dependency. External references stay references.",
  ],
  [
    "Impact",
    "Deterministic reverse traversal with shortest depths, cycle safety and explicit bounds.",
  ],
  [
    "Code Q&A",
    "Lexical and vector retrieval with graph expansion; answers cite numbered evidence or are not given.",
  ],
  [
    "Testing",
    "Graph-qualified test discovery, test generation from bounded evidence, validation, execution and recorded provenance.",
  ],
  [
    "Security",
    "OSV-Scanner and Semgrep findings, resolved to symbols, with graph blast radius kept separate from severity.",
  ],
  [
    "Automation",
    "Parent-revision deltas, impact reuse and invalidation planning for every new revision.",
  ],
  [
    "Documentation",
    "Repository, architecture, module and symbol documentation that cites evidence, with a deterministic fallback.",
  ],
  [
    "Investigations",
    "Sessions with messages and historical context references to symbols, files, runs and findings.",
  ],
];

export function LandingPage() {
  return (
    <div className={styles.page}>
      <LandingWorld />
      <RevealOnScroll containerId={CONTENT_ID} />

      <header className={styles.header}>
        <Link href="/" className={styles.brand} aria-label="DevLens home">
          <span className={styles.brandMark} aria-hidden="true" />
          DevLens
        </Link>
        <nav className={styles.nav} aria-label="Landing">
          <Link href="/#architecture" className={styles.navLink}>
            Architecture
          </Link>
          <Link href={WORKSPACE} className={styles.navCta}>
            Open workspace
          </Link>
        </nav>
      </header>

      <main id={CONTENT_ID} className={styles.content}>
        <section
          data-scene="intro"
          className={`${styles.scene} ${styles.intro}`}
        >
          <div className={styles.introInner}>
            <p className={styles.eyebrow}>Codebase intelligence</p>
            <h1 className={styles.title}>
              Understand your code
              <br />
              before you change it.
            </h1>
            <p className={styles.lede}>
              AI-powered codebase intelligence for understanding structure,
              dependencies, and change impact.
            </p>
            <div className={styles.actions}>
              <Link href={WORKSPACE} className={styles.primary}>
                Explore DevLens
              </Link>
              <Link href="/#architecture" className={styles.secondary}>
                View the architecture
              </Link>
            </div>
          </div>
          <p className={styles.scrollHint} aria-hidden="true">
            Scroll
          </p>
        </section>

        <section
          data-scene="code"
          className={styles.scene}
          aria-labelledby="scene-code"
        >
          <div className={styles.block} data-reveal>
            <p className={styles.step}>01 Source</p>
            <h2 id="scene-code" className={styles.heading}>
              It starts as text.
            </h2>
            <p className={styles.body}>
              DevLens ingests one Git revision and parses every Python file into
              symbols: modules, classes, functions and methods, each with its
              file, line span and signature.
            </p>
            <figure className={styles.specimen}>
              <ul>
                {specimen.map((fragment) => (
                  <li key={`${fragment.file}:${fragment.line}`}>
                    <code>{fragment.text}</code>
                    <span>
                      {fragment.file}:{fragment.line}
                    </span>
                  </li>
                ))}
              </ul>
              <figcaption>
                Every fragment in this scene is a real line of DevLens&apos;s
                own source.
              </figcaption>
            </figure>
          </div>
        </section>

        <section
          data-scene="structure"
          className={styles.scene}
          aria-labelledby="scene-structure"
        >
          <div className={`${styles.block} ${styles.right}`} data-reveal>
            <p className={styles.step}>02 Structure</p>
            <h2 id="scene-structure" className={styles.heading}>
              Your code already has a shape.
            </h2>
            <p className={styles.body}>
              Graph construction resolves calls, imports and inheritance into
              directed edges between symbols. An edge always points from the
              dependent to its dependency.
            </p>
            <dl className={styles.facts}>
              {Object.entries(provenance.edgesByRelationship).map(
                ([relationship, count]) => (
                  <div key={relationship}>
                    <dt>{relationship}</dt>
                    <dd>{number(count)}</dd>
                  </div>
                ),
              )}
            </dl>
            <p className={styles.note}>
              Distinct internal edges in DevLens&apos;s own backend, as built by
              its graph construction.
            </p>
          </div>
        </section>

        <section
          data-scene="world"
          className={styles.scene}
          aria-labelledby="scene-world"
        >
          <div className={styles.block} data-reveal>
            <p className={styles.step}>03 Dependency graph</p>
            <h2 id="scene-world" className={styles.heading}>
              DevLens maps the relationships hiding inside your repository.
            </h2>
            <p className={styles.body}>
              Every symbol belongs to exactly one revision, so the graph you
              explore is the graph of that commit. External references stay
              references; they never become invented nodes.
            </p>
            <p className={styles.note}>
              Shown: DevLens analyzing its own backend.{" "}
              {number(provenance.symbols)} symbols and{" "}
              {number(provenance.edges)} internal edges, clustered by package.{" "}
              <span className={styles.desktopOnly}>
                Point at a node to read its symbol.
              </span>
            </p>
          </div>
        </section>

        <section
          data-scene="impact"
          className={styles.scene}
          aria-labelledby="scene-impact"
        >
          <div
            className={`${styles.block} ${styles.right} ${styles.flow}`}
            data-reveal
          >
            <p className={styles.step}>04 Change impact</p>
            <h2 id="scene-impact" className={styles.heading}>
              Change one function.
              <br />
              See what breaks before you touch it.
            </h2>
            <p className={styles.body}>
              Impact analysis walks the graph backwards from the symbol you plan
              to change and collects every dependent at its shortest depth. It
              is deterministic: the same revision and symbol always produce the
              same impacted set. No model takes part.
            </p>
            <div className={styles.impact}>
              <p className={styles.impactSeed}>
                <span>Change</span>
                <code>
                  <SymbolName name={seedSymbol.name} />
                </code>
              </p>
              <ol className={styles.depths}>
                {depthGroups.map((group) => (
                  <li key={group.depth}>
                    <span className={styles.depthLabel}>
                      {group.depth} {group.depth === 1 ? "hop" : "hops"}
                    </span>
                    <ul>
                      {group.symbols.map((symbol) => (
                        <li key={symbol.name}>
                          <code>
                            <SymbolName name={symbol.name} />
                          </code>
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ol>
              <p className={styles.note}>
                The real impacted set for this function in DevLens&apos;s own
                backend: {dependents.length} dependents within{" "}
                {provenance.impactMaxDepth} hops, across automation,
                documentation, Q&amp;A and their API routes.
              </p>
            </div>
          </div>
        </section>

        <section
          data-scene="intelligence"
          className={styles.scene}
          aria-labelledby="scene-intelligence"
        >
          <div
            className={`${styles.block} ${styles.wide} ${styles.right} ${styles.flow}`}
            data-reveal
          >
            <p className={styles.step}>05 Explanation</p>
            <h2 id="scene-intelligence" className={styles.heading}>
              AI explains the evidence.
              <br />
              It doesn&apos;t invent the graph.
            </h2>
            <p className={styles.body}>
              The impacted set is computed first. A model is only then asked to
              explain it, and only from numbered evidence DevLens retrieved
              itself.
            </p>
            <div className={styles.contract}>
              <div className={styles.facts2}>
                <p className={styles.panelLabel}>Deterministic evidence</p>
                <ol>
                  {evidence.map((item, i) => (
                    <li key={item.name}>
                      <span className={styles.evidenceId}>E{i + 1}</span>
                      <span>
                        <code>
                          <SymbolName name={item.name} />
                        </code>
                        <span className={styles.location}>
                          {item.file}:{item.lines[0]}-{item.lines[1]}
                        </span>
                      </span>
                    </li>
                  ))}
                </ol>
              </div>
              <div className={styles.generated}>
                <p className={styles.panelLabel}>Generated explanation</p>
                <ul>
                  <li>Cites evidence by id, as [E1] or [E2].</li>
                  <li>
                    Citations to ids that were never supplied are stripped.
                  </li>
                  <li>
                    The response schema rejects unknown fields, so a model
                    cannot add nodes, edges or scores.
                  </li>
                  <li>
                    If the provider fails, the deterministic result stands
                    unchanged.
                  </li>
                </ul>
              </div>
            </div>
          </div>
        </section>

        <section
          data-scene="quality"
          className={styles.scene}
          aria-labelledby="scene-quality"
        >
          <div
            className={`${styles.block} ${styles.wide} ${styles.flow}`}
            data-reveal
          >
            <p className={styles.step}>06 Quality signals</p>
            <h2 id="scene-quality" className={styles.heading}>
              Tests and findings, tied to the same graph.
            </h2>
            <div className={styles.columns}>
              <div>
                <h3 className={styles.subheading}>Testing</h3>
                <ul className={styles.list}>
                  <li>
                    Finds existing tests through call and import evidence, never
                    by file name alone.
                  </li>
                  <li>
                    Generates a test for a target from bounded evidence, then
                    validates its path and syntax.
                  </li>
                  <li>
                    Can execute it in a subprocess with a timeout and records
                    which provider and model produced it.
                  </li>
                </ul>
              </div>
              <div>
                <h3 className={styles.subheading}>Security</h3>
                <ul className={styles.list}>
                  <li>
                    OSV-Scanner for dependency advisories, Semgrep for static
                    findings.
                  </li>
                  <li>
                    Findings resolve to symbols; blast radius follows the
                    dependency graph and is reported apart from severity.
                  </li>
                  <li>
                    A scanner that could not run is reported as unavailable,
                    never as clean.
                  </li>
                </ul>
              </div>
            </div>
          </div>
        </section>

        <section
          data-scene="automation"
          className={styles.scene}
          aria-labelledby="scene-automation"
        >
          <div className={`${styles.block} ${styles.right}`} data-reveal>
            <p className={styles.step}>07 Automation</p>
            <h2 id="scene-automation" className={styles.heading}>
              Every revision becomes intelligence.
            </h2>
            <p className={styles.body}>
              For a new revision, Automation resolves its real Git parent and
              plans what the change invalidates.
            </p>
            <ol className={styles.pipeline}>
              <li>
                <span>Changed files</span>content-hash delta against the parent
                revision
              </li>
              <li>
                <span>Changed symbols</span>signature and body comparison,
                limited to changed files
              </li>
              <li>
                <span>Graph impact</span>reverse traversal from every changed
                symbol
              </li>
              <li>
                <span>Retrieval</span>the revision&apos;s code chunks are
                rebuilt for Q&amp;A
              </li>
              <li>
                <span>Tests and security</span>stale generated tests and scanner
                re-runs are identified, not run blindly
              </li>
            </ol>
            <p className={styles.note}>
              Parsing, graph construction and chunking run as full-revision
              rebuilds. There is no partial mode.
            </p>
          </div>
        </section>

        <section
          id="architecture"
          data-scene="architecture"
          className={styles.scene}
          aria-labelledby="scene-architecture"
          tabIndex={-1}
        >
          <div
            className={`${styles.block} ${styles.wide} ${styles.flow}`}
            data-reveal
          >
            <p className={styles.step}>08 Architecture</p>
            <h2 id="scene-architecture" className={styles.heading}>
              One pipeline, one revision at a time.
            </h2>
            <p className={styles.body}>
              Each stage reads the persisted output of the stage before it, and
              everything it stores is scoped to a repository and a revision.
            </p>
            <dl className={styles.stages}>
              {ARCHITECTURE.map(([name, description]) => (
                <div key={name}>
                  <dt>{name}</dt>
                  <dd>{description}</dd>
                </div>
              ))}
            </dl>
            <p className={styles.note}>
              FastAPI, SQLAlchemy and PostgreSQL with pgvector on the backend; a
              Next.js workstation on the frontend.
            </p>
          </div>
        </section>

        <section
          data-scene="finale"
          className={`${styles.scene} ${styles.finale}`}
          aria-labelledby="scene-finale"
        >
          <div className={styles.finaleInner} data-reveal>
            <p className={styles.wordmark}>
              <span className={styles.brandMark} aria-hidden="true" />
              DevLens
            </p>
            <h2 id="scene-finale" className={styles.finaleTitle}>
              Understand before you change.
            </h2>
            <div className={styles.actions}>
              <Link href={WORKSPACE} className={styles.primary}>
                Explore DevLens
              </Link>
              <Link href="/#architecture" className={styles.secondary}>
                View the architecture
              </Link>
            </div>
          </div>
        </section>
      </main>

      <footer className={styles.footer}>
        <p>
          Every graph on this page is DevLens&apos;s own backend, analyzed by
          DevLens. Symbols, edges and the impacted set are product output; only
          the 3D positions are a layout.
        </p>
      </footer>
    </div>
  );
}
