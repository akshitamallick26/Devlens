# Repository Ingestion (Phase 3, v1)

This document covers the deterministic ingestion foundation: turning a Git
repository URL into `Repository` / `RepositoryRevision` / `SourceBlob` /
`SourceFile` rows, per `docs/architecture/domain-model.md` and
`docs/architecture/database-design.md`. It implements those documents; it
does not change them. No parsing, graph construction, RAG, security
scanning, or automation exists yet — see "Non-goals" below.

## 1. Ingestion goals

- Given a public GitHub HTTPS URL, deterministically produce: one
  `Repository` row (identity), one `RepositoryRevision` row (the exact
  immutable commit ingested), and `SourceFile`/`SourceBlob` rows for every
  file DevLens can meaningfully store, in deterministic order.
- Re-ingesting the same commit is idempotent — it does not create duplicate
  rows or duplicate blob content.
- A failed ingestion never leaves a repository marked `ready`, and never
  leaves partial file content that looks complete.
- Treat the input (a URL, and the repository content it points to) as
  **untrusted** throughout.

## 2. Non-goals for this phase

Explicitly out of scope, deferred to later phases named in
`docs/architecture/domain-model.md`'s roadmap: AST parsing, tree-sitter,
symbol extraction, dependency/call graph construction, RAG, embeddings, LLM
calls, security scanning, test generation, background job workers/queues,
GitHub OAuth / private repositories, PR analysis, frontend UI, automation or
watchers. The `/repositories/ingest` endpoint added here runs synchronously
and is the only HTTP surface this phase adds.

## 3. Trust/security boundaries

Two untrusted inputs cross a boundary here:

1. **The URL itself** — attacker-controlled input that could attempt SSRF
   (reach internal/private network targets), command injection (if ever
   passed through a shell), or credential leakage (if it embeds a token).
   See §4 and Task 2's module for how each is closed off.
2. **The repository's content** — once cloned, file paths, file sizes, file
   count, and byte content are all attacker-influenced. Path traversal,
   decompression-bomb-style oversized files, symlinks pointing outside the
   checkout, and sheer volume are all treated as adversarial, not assumed
   benign just because the host is `github.com`.

A third, narrower risk is **DNS rebinding**: validating that a hostname
string equals `github.com` does not guarantee the IP address `git` actually
connects to at clone time is a GitHub IP — a DNS response could differ
between validation and connection. This is a known, **unmitigated** residual
risk in v1 (closing it properly needs IP-pinning the resolved connection,
which is disproportionate infrastructure for this phase) and is called out
here rather than silently assumed away.

## 4. Repository URL normalization rules

Implemented in `app/ingestion/url_policy.py`. A URL is normalized to a
`RepositoryIdentity(host, owner, name, canonical_url, clone_url)` or rejected
with a typed `RepositoryURLError` subclass. Rejected, in order checked:

1. Scheme other than `https` (rejects `http://`, `file://`, `git://`,
   `ssh://`, `ftp://`, etc.) — `UnsupportedSchemeError`.
2. Embedded credentials (`https://user:pass@github.com/...`) —
   `EmbeddedCredentialsError`.
3. Host that is `localhost`, a loopback address, a private-network address
   (RFC 1918 / link-local / unique-local), or any literal IP address at all
   — `PrivateNetworkHostError`. Checked before the host allowlist so the
   error is specific, even though the allowlist below would also reject
   these.
4. Host that isn't exactly `github.com` (case-insensitive) — v1 is
   GitHub-only; see §5 — `UnsupportedHostError`.
5. A query string or fragment present at all — GitHub repository identity
   never includes one, so presence is treated as malformed input rather than
   silently stripped — `UnsupportedURLComponentError`.
6. A path that isn't exactly `/OWNER/REPO` or `/OWNER/REPO.git` (no extra
   segments, no trailing content after an optional `.git` suffix) —
   `MalformedRepositoryPathError`.
7. `OWNER` or `REPO` that doesn't match a conservative allowed character set
   (`[A-Za-z0-9._-]`, bounded length) — this charset has no `/`, which is
   what actually rules out path traversal (`..` as a path segment requires a
   `/` to matter; `..` as a bare string inside a single segment is just a
   name, not a traversal) — `MalformedRepositoryPathError`.

A trailing slash and an optional `.git` suffix are stripped as part of
normalization, not treated as separate URL forms.

## 5. Supported URL forms for v1

GitHub only, HTTPS only, public repositories only:

- `https://github.com/OWNER/REPO`
- `https://github.com/OWNER/REPO.git`
- Either of the above with a single trailing slash.

Nothing else is accepted in v1 — not SSH URLs (`git@github.com:...`), not
other hosts (GitLab, Bitbucket, self-hosted Git), not GitHub Enterprise.
`docs/architecture/domain-model.md` and `database-design.md` don't make any
host-specific assumptions in the schema (`Repository.owner`/`.name`/`.url`
are host-agnostic strings), so extending to other hosts later is a change
confined to `url_policy.py` plus the Git adapter's clone-URL construction —
not a schema change. That's the extension point; it is not built now.

## 6. Checkout strategy

A fresh `git clone --depth 1 --no-tags --single-branch --no-recurse-submodules`
into a `tempfile.mkdtemp()` directory **outside the application source
tree**, with:

- `GIT_LFS_SKIP_SMUDGE=1` — no LFS object downloads.
- No submodule recursion (`--no-recurse-submodules`) — which also means
  `.gitmodules` is never read during this clone, so there's no secondary
  protocol-resolution surface (e.g. a malicious submodule URL pointing at
  `file://`) for this single invocation to restrict further.
- A minimal, credential-free, deterministic environment: global/system
  gitconfig disabled (`GIT_CONFIG_GLOBAL=/dev/null`,
  `GIT_CONFIG_SYSTEM=/dev/null`), no terminal credential prompts
  (`GIT_TERMINAL_PROMPT=0`).
- An explicit timeout on the clone subprocess (`ingestion_clone_timeout_seconds`).

The Git adapter's `clone_repository()` does not itself enforce HTTPS-only —
that's `url_policy.normalize_repository_url()`'s job, called once before any
clone happens, in `ingest_repository()`. This separation is deliberate: it's
what lets tests exercise the real clone/checkout/write pipeline against a
local filesystem path (see `tests/ingestion/conftest.py`'s repo fixture)
without needing real network access, while production traffic always goes
through the URL policy gate first. See §5's "supported URL forms" — nothing
else is accepted at the point requests actually enter the system.

The clone directory is removed (`shutil.rmtree`) in a `finally` block
regardless of whether ingestion succeeds, fails, or raises partway through —
implemented as a context manager (`checkout_repository`) so cleanup can't be
skipped by a caller forgetting to call it.

## 7. Immutable revision identity

After clone, `git rev-parse HEAD` resolves the exact commit. That value is
validated as 40 or 64 lowercase hex characters (Git's SHA-1 object format is
40; SHA-256 repositories, which GitHub doesn't yet use but Git itself
supports, would be 64 — both are accepted rather than hardcoding one) before
it is ever treated as `RepositoryRevision.commit_sha`. **The branch name is
never treated as revision identity** — it's recorded separately
(`RepositoryRevision.branch_ref`, informational only) via
`git rev-parse --abbrev-ref HEAD`, which also supplies `Repository.default_branch`
on first ingestion (a plain `git clone` with no explicit branch checks out
the remote's actual default branch).

## 8. File discovery rules

`git ls-files` inside the checkout — **not** `os.walk` — so the inventory
is exactly Git's own tracked-file view. This has a direct, useful
consequence: anything `.gitignore`'d and never committed (`node_modules/`,
build output, etc.) never appears in the list at all, with zero extra logic
required. Output is NUL-separated (`git ls-files -z`) specifically so
filenames containing spaces, newlines, or other unusual bytes are handled
safely rather than split incorrectly. Paths are sorted (`git ls-files`
already returns them in a deterministic, sorted order; the code sorts again
explicitly rather than relying on that implicitly). Every path is verified
to be relative, POSIX-style, and free of `..` segments before use — a
defensive check, since `git ls-files` shouldn't ever produce such a path,
but this is explicitly an untrusted-input boundary (§3).

## 9. Ignored/generated/binary file policy

Classification (`app/ingestion/inventory.py`) produces one of:

- **TEXT** — stored as a `SourceFile` + `SourceBlob`. May additionally be
  flagged `is_generated` (a short, non-exhaustive pattern list — common
  lockfiles by exact filename, `*.min.js`/`*.min.css` — not a language
  taxonomy, just a hint for later phases to deprioritize).
- **BINARY** — detected deterministically (a NUL byte in the first 8 KB of
  the file, the same heuristic Git itself uses to decide "is this binary"
  for diffs — no LLM, no magic-byte library). **Never stored** —
  `SourceBlob` is for content DevLens can meaningfully analyze, per the
  explicit instruction for this phase.
- **UNSUPPORTED** — a file that is a symlink (could point outside the
  checkout — rejected as a path-escape risk, not followed) or exceeds
  `ingestion_max_file_bytes`. Never stored.

**v1 does not persist a database row for skipped files.** `SourceFile.content_hash`
is `NOT NULL`, so a file with no storable content cannot get a `SourceFile`
row without inventing a placeholder blob, which would misrepresent source
truth. Skipped files are reported in the ingestion summary (counts, and the
failed `AnalysisRun.error_message`/log events only on unrecoverable
failures) but not in `source_files`. A future phase could add an explicit
skip ledger if that turns out to matter for the Files UI; it isn't built
now.

A small (~20-entry), explicitly bounded extension-to-language map feeds
`SourceFile.language` for common languages (Python, TypeScript/JavaScript,
Go, Rust, Java, Ruby, C/C++, etc.) — not a taxonomy, just enough metadata
for the next phase to route files to the right parser. Anything unmapped
gets `language = NULL`, which the schema already supports.

## 10. SourceBlob content-addressing strategy

`content_hash = sha256(file_bytes).hexdigest()` — **SHA-256, not Git's own
SHA-1 blob hash.** This matches the schema as already built:
`source_blobs.content_hash` and `source_files.content_hash` are both
`VARCHAR(64)`, which is a SHA-256 hex digest's exact length (Git blob SHA-1
would be 40 hex characters and wouldn't fit the documented intent). Using a
different algorithm than what the column width implies would be the "silent
new hash format" this task explicitly warned against, so this was verified
against `app/models/source.py` directly before writing any ingestion code.

Within one ingestion run, blobs are deduplicated via an in-memory
`dict[str, SourceBlob]` cache plus `Session.get()` (which also dedupes
against blobs from *other* repositories/revisions already in the database,
not just within this run) before deciding whether to insert a new
`SourceBlob` row — satisfying "the same content across different revisions
resolves to the same SourceBlob" directly.

All v1 content is stored via `SourceBlob.inline_content` (the `TEXT` column)
— `storage_key` (external/object storage) is left `NULL` for every row.
Per `database-design.md`, the inline-vs-external threshold is an
application/config decision never made in the schema; v1 makes it trivially
by not implementing external storage at all yet. `ingestion_max_file_bytes`
is small enough (default 1 MB) that inline storage is reasonable for now.

## 11. SourceFile revision snapshot semantics

One `SourceFile` row per `(revision_id, path)`, created once, never updated
— consistent with the immutability discussion in `domain-model.md` §13.
Re-ingesting the *same* commit is idempotent and does not touch existing
`SourceFile` rows at all (see §12); ingesting a *different* commit of the
same repository creates an entirely new set of `SourceFile` rows under the
new `revision_id`, reusing existing `SourceBlob` rows wherever content is
unchanged.

## 12. Transaction boundaries

1. Get-or-create the `Repository` row, set `status = 'ingesting'` — committed
   immediately (cheap, and makes the attempt visible even if everything
   after this fails).
2. Checkout + clone + inventory discovery happens with **no database writes
   at all** — pure filesystem/subprocess work. A failure here (bad network,
   clone timeout, clone failure) sets `repository.status = 'error'` and
   stops; there is no commit identity yet to attach an `AnalysisRun` or
   `RepositoryRevision` to.
3. Once the commit SHA is known: check for an existing `RepositoryRevision`
   for `(repository_id, commit_sha)`. If found, this is the idempotent path
   — update `repository.current_revision_id`/`status = 'ready'` and return;
   no new rows are written.
4. Otherwise, create the new `RepositoryRevision` row and an
   `AnalysisRun(analysis_type='ingestion', status='running')` row, and
   **commit them together, immediately** — this is deliberate: the revision
   row itself is durable and cheap, and keeping it independent of the much
   larger file-writing transaction means a failure in step 5 still leaves a
   clear, queryable record ("we discovered this commit, and ingestion of its
   content failed") instead of erasing all evidence of the attempt.
5. All `SourceBlob`/`SourceFile` writes for the revision happen in one
   transaction. On success, that same transaction also sets
   `revision.file_count`, `revision.latest_analysis_status = 'completed'`,
   the `AnalysisRun` to `'completed'`, and `repository.status = 'ready'` /
   `current_revision_id` — one commit, all or nothing. On any exception, the
   whole transaction is rolled back (every `SourceFile`/`SourceBlob` from
   this attempt disappears), and a **separate**, small transaction records
   `AnalysisRun.status = 'failed'` (with `error_message`),
   `revision.latest_analysis_status = 'failed'`, and
   `repository.status = 'error'`.

Net effect: a `RepositoryRevision` can exist with zero `SourceFile` rows
(a recorded, failed attempt) but a `Repository` is never `'ready'` while
pointing at a revision whose content wasn't fully written.

## 13. Failure/cleanup behavior

- Checkout cleanup: guaranteed via a context manager's `finally`, regardless
  of where in the `with` block an exception originates.
- Database cleanup: `db.rollback()` before recording the failure state (§12).
- No partial file content is ever stored: a file either gets its full
  (size-bounded) content hashed and stored, or it gets no row at all — never
  a truncated row pretending to be complete (explicit instruction for this
  phase).
- Every raised error is a typed exception (`RepositoryURLError` subclasses,
  `GitCommandError`/`GitTimeoutError`, `IngestionLimitExceededError`) so the
  API layer can map each to an appropriate HTTP status without string
  matching.

## 14. Maximum repository/file limits for v1

All configurable via `Settings` (`app/core/config.py`), with conservative
defaults:

| Setting | Default | Meaning |
|---|---|---|
| `ingestion_clone_timeout_seconds` | 120 | Hard timeout on the clone subprocess |
| `ingestion_git_command_timeout_seconds` | 30 | Timeout for every other Git subprocess call |
| `ingestion_max_files` | 20,000 | Tracked-file count ceiling, checked before any content is read — a repo over this limit fails fast |
| `ingestion_max_file_bytes` | 1,000,000 (1 MB) | Per-file size ceiling; files over this are classified UNSUPPORTED and skipped, not truncated |
| `ingestion_max_total_bytes` | 200,000,000 (200 MB) | Cumulative stored-content ceiling for one ingestion; exceeding it fails the whole ingestion (§12/§13) rather than silently stopping partway |

These are deliberately conservative v1 numbers for a foundation phase, not a
claim about what DevLens should ultimately support.

## 15. Future extension points

Not built now, explicitly deferred:

- Additional Git hosts (GitLab, Bitbucket, self-hosted) — confined to
  `url_policy.py` and clone-URL construction if/when needed (§5).
- Private/authenticated repository access (OAuth, deploy keys, tokens).
- External blob storage (`SourceBlob.storage_key`) for large files, once a
  real size/cost threshold is decided (`domain-model.md` §16).
- A skip ledger for BINARY/UNSUPPORTED files if the Files UI later needs to
  show "this file exists but wasn't ingested, here's why."
- Async/background ingestion (current v1 endpoint is synchronous, bounded by
  the limits in §14).
- IP-pinning the clone connection to fully close the DNS-rebinding gap noted
  in §3.
