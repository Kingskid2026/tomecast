# Contributing to Tomecast

Thanks for helping! This guide covers everything from a first bug report to a merged pull request.

## Ways to help

- **Report a bug** with the [bug report form](https://github.com/Kingskid2026/tomecast/issues/new?template=bug_report.yml). Include your distro and graphics card.
- **Suggest a feature** with the [feature request form](https://github.com/Kingskid2026/tomecast/issues/new?template=feature_request.yml).
- **Test on your hardware.** AMD graphics cards, older laptops and less common distros are especially useful.
- **Send a pull request.** Issues labelled `good first issue` are a good place to start.

## Set up a development copy

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and git. uv installs the right Python for you.

```sh
git clone https://github.com/Kingskid2026/tomecast.git
cd tomecast
uv sync                 # creates .venv with Tomecast and the dev tools
uv run tomecast --help  # run your working copy
```

## Before you open a pull request

Run the same checks CI runs:

```sh
uv run ruff check           # lint
uv run ruff format          # auto-format (CI runs `ruff format --check`)
uv run mypy                 # type checks (strict)
uv run pytest               # tests
```

If your change affects audio, **listen to the result** and say what you checked in the pull request.

## Project rules

1. **Every sentence gets spoken.** Never send a whole page or chapter to a voice model in one go, because models silently cut off long input. Text goes to the voice in small, size-capped chunks, and every chunk is checked for audio before packaging. See "Every sentence gets spoken" in [docs/architecture.md](docs/architecture.md).
2. **No copyrighted books in the repo.** Test books come only from the public domain ([Project Gutenberg](https://www.gutenberg.org/), [Standard Ebooks](https://standardebooks.org/), the [Internet Archive](https://archive.org/)'s public-domain scans). Keep your own books in `local/`, which git ignores.
3. **No model or engine files in the repo.** Tomecast downloads them from their official sources, pinned by version and SHA-256 checksum.
4. **Unit tests don't use the network.** Anything that downloads goes in a separately marked integration test.
5. **Built-in voices only.** No cloning of real people's voices.
6. **Keep the docs true.** If you change the design, update [docs/architecture.md](docs/architecture.md) in the same pull request.

## Commit messages

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/). The release bot reads them to pick the next version number and write the changelog:

| Prefix | Use it for | Effect on the next release |
|---|---|---|
| `feat:` | A new feature | Minor bump (patch bump while we're below 1.0) |
| `fix:` | A bug fix | Patch bump |
| `docs:`, `test:`, `refactor:`, `chore:`, `ci:` | Everything else | No release on its own |
| `feat!:` or a `BREAKING CHANGE:` footer | A breaking change | Major bump (minor while below 1.0) |

Example: `feat: read chapter titles from the EPUB table of contents`

## How releases work

Maintainers don't tag releases by hand:

1. Pull requests merge into `main` once CI passes.
2. [release-please](https://github.com/googleapis/release-please) keeps an open "release" pull request that collects the changes and writes `CHANGELOG.md`.
3. Merging that pull request creates the version tag and the GitHub release, then builds the package and publishes it to [PyPI](https://pypi.org/project/tomecast/) through Trusted Publishing, with no stored passwords.

Before a release, a maintainer runs the GPU checks on real hardware, because GitHub's CI machines have no graphics card.
