"""The optional local AI pass (off by default).

Nothing outside this package imports it unless the user turns the AI pass on, so a normal
conversion never starts a model. See docs/architecture.md, "Optional AI pass".

- :mod:`tomecast.ai.judge`: judgment calls on numbered lines (keep, drop, chapter start). The
  model only names line numbers; the app makes the edits and checks that no kept line changed.
- :mod:`tomecast.ai.llama`: runs ``llama-server`` (llama.cpp) behind a small interface.
- :mod:`tomecast.ai.assets`: the pinned llama.cpp build and model files.
"""
