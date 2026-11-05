# Security policy

## Supported versions

Only the latest release of Tomecast gets security fixes.

## Reporting a vulnerability

**Please don't open a public issue for security problems.** Report them privately through GitHub instead:

1. Go to the [Security tab](https://github.com/Kingskid2026/tomecast/security) of this repository.
2. Click **Report a vulnerability**.

You should get a reply within a week. Once a fix is released, you'll be credited in the release notes (unless you'd rather not be).

## What's in scope

Tomecast downloads and runs third-party engines and models, so these matter most:

- the install script (`install.sh`)
- how engines and models are downloaded and verified (pinned versions and SHA-256 checksums)
- reading untrusted ebook files (EPUB, PDF, DOCX, FB2, HTML, TXT)
- the update check

Bugs in the upstream engines or models themselves (llama.cpp, audio.cpp, Kokoro and so on) should go to those projects. If you're unsure, report it here.
