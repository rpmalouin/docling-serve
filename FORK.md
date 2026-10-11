# FORK.md — Why this fork exists

**This is `rpmalouin/docling-serve`**, a self-hosted deployment fork of upstream
[docling-serve](https://github.com/docling-project/docling-serve) (the FastAPI service + React UI
that runs [Docling](https://github.com/docling-project/docling) as an HTTP API). It serves the
homelab `docling` stack and is built into the running container by
`/appdata/A--docker_stacks/docling/Dockerfile.fork`; the engine side of that stack is the sibling
fork `/appdata/docling` (`docling-slim`).

Baseline: our `main` started byte-identical to upstream `main` at **v1.36.0**
(`07b1d3d`, verified by GitHub compare — 0 ahead / 0 behind). Everything below is ours.

## Why a fork

1. **Google Docs as an input source.** Upstream converts files and plain HTTP(S) URLs; a Google
   Docs/Sheets/Slides link is neither. docling-core already rewrites `docs.google.com` URLs to
   Google's export endpoint, but the serve layer named every export `export`, reported an
   inaccessible document as `410 Client Error: Gone`, mis-parsed published `/d/e/…` links, and
   failed outright on the `.gdoc`/`.gsheet`/`.gslides` shortcut files Google Drive syncs in place
   of a document. This fork makes Google Docs a first-class input, including the owner's private
   files (optional credentials).
2. **Deployment.** The published image installs `docling-serve` as an *editable* package rooted at
   `/opt/app-root/src`, so upstream code is what runs unless the fork's `docling_serve/` is
   overlaid onto that path at image build time — which is what `Dockerfile.fork` now does. Without
   the fork the service cannot carry serve-layer changes at all.
3. **Agent tooling.** `.code-review-graph/` (graph-first code exploration) and `MEMORY.md`
   (gitignored operational notes) travel with the fork.

## Key changes in this fork vs upstream

| Area | Change | Commits |
|------|--------|---------|
| **Google Docs input** | New `docling_serve/gdocs.py`: parse/validate a Google Docs/Sheets/Slides/Drive link, fetch Google's export (`docx`/`xlsx`/`pptx`), name the result from `Content-Disposition` (read case-insensitively — the case-sensitive lookup in docling-core is why every export was named `export`), enforce the size cap, and map failures to a clear `422`. Hooked into the convert/batch/chunk request preparation in `docling_serve/app.py`. | `603c90a` |
| **Drive shortcut files** | `.gdoc`/`.gsheet`/`.gslides` uploads (a ~170-byte JSON pointer: `{"doc_id": …, "resource_key": "", "email": …}`) are resolved to the linked document in `_enqueue_file`; `.gform`/`.gdraw`/`.gscript`/`.gjam`/`.gtable` are refused with a message naming the product. `open?id=` links accepted; `resource_key` carried into the export URL with a no-key retry. Also stops re-materializing a source that already points at Google's export endpoint, and forwards caller headers — otherwise a client's `Authorization` token was dropped. | `23b37cf` |
| **Optional Google credentials** | `DOCLING_SERVE_GDOC_CLIENT_ID` / `_CLIENT_SECRET` / `_REFRESH_TOKEN` (secret values never logged): a lazily refreshed, process-cached bearer token is sent on the export request so the account's own private documents convert. A caller-supplied `Authorization` header wins; attempts fall back to unauthenticated so a broken credential cannot block a public document. Unconfigured = the original unauthenticated behavior. | `f38e860` |
| **UI** | A third "Google Doc" source tab in `ui/src/components/SourceInput.tsx` (sharing hint, inline warnings for published/resource-key links) and a File-tab hint about shortcut files. The wire format stays `{"kind":"http","url":…}`. | `603c90a`, `23b37cf` |
| **Tests** | `tests/test_gdocs_{url,stub,materialize,endpoint}.py` — 88 tests covering URL/stub parsing, export mapping, filename resolution, the key/credential retry matrix, size limits, and the endpoint paths. | all three |

Not a fork change: nothing under `docling_serve/datamodel/`, `policy.py`, `capabilities.py` or the
engine's request schemas — `SourceRequestItem` is still `file | http`, so API clients see no schema
change.

## Deployment (how this fork reaches the running container)

The live stack is `/appdata/A--docker_stacks/docling` (compose is the source of truth; Dockhand
reconciles). The fork's code arrives through a second build layer:

```sh
cd /appdata/docling-serve/ui && npm install && npm run build      # vite → docling_serve/ui_static
tar czf /appdata/A--docker_stacks/docling/dist/docling-serve-src.tgz \
    --exclude='__pycache__' -C /appdata/docling-serve docling_serve
cd /appdata/A--docker_stacks/docling && docker compose up -d --build docling-serve
docker exec docling-serve python -c "import docling_serve; print(docling_serve.__file__)"   # /opt/app-root/src/docling_serve
```

Tests run against the built image (the image has no `pytest` and the host has no venv):

```sh
docker run --rm -v /appdata/docling-serve:/src -w /src docling-serve:local \
  sh -c "pip install -q pytest 'pytest-asyncio~=1.2' && python -m pytest tests/test_gdocs_*.py -q"
```

## Upstream sync

Upstream is not configured as a remote (only `origin` = this fork). To sync:

```sh
git remote add upstream https://github.com/docling-project/docling-serve   # once
git fetch upstream && git rebase upstream/main
```

Expect conflicts where we touch the same files: `docling_serve/app.py` (the three `_prepare_*`
helpers are `async` here and call `normalize_gdoc_sources`; upstream may add call sites that need
`await`) and `docling_serve/settings.py` (our three `gdoc_*` fields). `docling_serve/gdocs.py` and
the `test_gdocs_*` files are new, so they only conflict if upstream adds the same names. Keep the
diff small and reviewable — it is the whole cost of the next rebase. No upstream PRs: these changes
are fork-only by convention.

After any sync: re-run the tests above, rebuild the image, then re-run the code-review graph
(`build_or_update_graph_tool` with `repo_root=/appdata/docling-serve`) and confirm
`built_at_sha == git rev-parse HEAD`.

## Repository conventions

- **Remote:** `origin` = `https://github.com/rpmalouin/docling-serve`; branch `main` kept.
- **Git identity:** `Ron Malouin <ron@malouin.com>` (set per commit; there is no repo-local default).
- **`FORK.md` is tracked** (this file); **`MEMORY.md` is gitignored** — operational notes, never
  upstream content. `.code-review-graph/` self-ignores.
- **No new dependencies**: everything used (`httpx`, `google-auth` via docling-jobkit's gdrive extra)
  ships in the image already.
- **Verification before pushing:** the gdocs test set green in the built image, `npm run typecheck`
  for UI changes, and a live conversion through the rebuilt container.
