# AGENTS.md

Instructions for AI coding agents (Claude Code, Codex, and others) working in this repo. Humans: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Start here

1. Read [docs/architecture.md](docs/architecture.md). It is the source of truth for the design and the reasons behind each model and quantization choice.
2. Check the [open issues and the current milestone](https://github.com/Kingskid2026/tomecast/milestones) for what to work on.

## Commands

```sh
uv sync                     # set up .venv with dev tools
uv run ruff check           # lint
uv run ruff format          # format
uv run mypy                 # strict type checks
uv run pytest               # tests
uv build                    # build sdist + wheel
```

Run all four checks (ruff check, ruff format --check, mypy, pytest) before saying a change is done.

## Rules

- **Git:** work on a branch and open a pull request. Never commit straight to `main`.
- **Commit messages:** use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`, `ci:`). release-please depends on them.
- **No AI attribution:** don't add `Co-Authored-By` trailers for AI tools, or "Generated with …" lines, to commits or pull requests. This is the maintainer's explicit preference.
- **Every sentence gets spoken:** never send a whole page or chapter to a voice model in one call. Use size-capped chunks, keep the trailing fragment, give every sentence an ID, and fail loudly (or regenerate) if any sentence has no audio. Any change to readers, chunking or synthesis needs a test that proves no text is lost.
- **No copyrighted books, and no model or engine files, in the repo.** Test books are public domain only. Models and engines are downloaded, pinned by version and SHA-256.
- **No network in unit tests.** Mark integration tests that download things.
- **Types:** everything is typed, and mypy runs in strict mode.
- **Docs:** if a change alters the design, update `docs/architecture.md` in the same pull request.
- **Visual design is the maintainer's:** the desktop window follows `docs/design.md`. Don't invent or restyle the look. Offer options, and let the maintainer choose.
- **Release pull requests** (from release-please) get no CI runs, because GitHub doesn't run workflows on pull requests a bot opened. They only bump the version and changelog, so a maintainer merges them with the admin bypass (`gh pr merge N --squash --admin`). Never use the bypass for any other pull request.
- **Licenses:** Tomecast is Apache 2.0. Don't add AGPL/GPL Python dependencies (for example, use `pypdfium2`, not PyMuPDF). When you add an engine or model, add it to `THIRD_PARTY.md`.
