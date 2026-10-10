# DevLens — web

The DevLens frontend: Next.js (App Router) + TypeScript. See the
[repository root README](../../README.md) for the product, architecture and
setup across all of `apps/`.

- `/` is the landing page. Its WebGL scene (`src/components/landing/`) uses
  Three.js and renders DevLens's own backend graph from a static export; it
  falls back to a static 2D drawing under reduced motion or without WebGL,
  and makes no API calls.
- `/workspace` opens or ingests a repository.
- `/repositories/[repositoryId]/...` is the workstation: overview,
  architecture, files, impact, Q&A, testing, security, change analysis,
  documentation and investigations. It reads the API through the typed client
  in `src/lib/api/`; page loads only issue GET requests.

Configuration: `NEXT_PUBLIC_API_URL` (see `.env.example`).

- **Node**: 24 (see root `.nvmrc`)
- **Package manager**: pnpm (see `packageManager` in `package.json`)

```bash
pnpm install
pnpm dev             # start the dev server (http://localhost:3000)
pnpm test            # vitest
pnpm typecheck       # next typegen && tsc --noEmit
pnpm lint            # eslint
pnpm format:check    # prettier --check .
pnpm build           # production build
```
