"""The llama-server runner's command line, request and reply handling. No server runs here."""

from __future__ import annotations

from pathlib import Path

import pytest

from tomecast.ai import assets
from tomecast.ai.judge import Reply, schema
from tomecast.ai.llama import LlamaError, LlamaServer, parse_response, request_body, server_command


def test_server_command_runs_on_cpu_with_an_8_bit_cache_and_a_capped_context() -> None:
    command = server_command(
        Path("/x/llama-server"), Path("/m/model.gguf"), port=4321, threads=2, context=8192
    )
    pairs = dict(zip(command[1::2], command[2::2], strict=False))
    assert command[0] == "/x/llama-server"
    assert pairs["--model"] == "/m/model.gguf"
    assert pairs["--host"] == "127.0.0.1"
    assert pairs["--threads"] == pairs["--threads-batch"] == "2"
    assert pairs["--ctx-size"] == "8192"
    assert pairs["--cache-type-k"] == pairs["--cache-type-v"] == "q8_0"
    assert pairs["--n-gpu-layers"] == "0"
    assert pairs["--parallel"] == "1"


def test_request_is_greedy_held_to_the_schema_and_without_thinking() -> None:
    body = request_body("sys", "lines", schema(3), 50)
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 50
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "verdicts", "strict": True, "schema": schema(3)},
    }
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "lines"},
    ]


def test_parse_response_reads_the_text_and_timings() -> None:
    payload = {
        "choices": [{"message": {"role": "assistant", "content": '{"drop": []}'}}],
        "timings": {"prompt_n": 120, "prompt_ms": 400.5, "predicted_n": 9, "predicted_ms": 90},
    }
    assert parse_response(payload) == Reply('{"drop": []}', 120, 9, 400.5, 90.0)


def test_parse_response_without_timings() -> None:
    payload = {"choices": [{"message": {"content": "{}"}}]}
    assert parse_response(payload) == Reply("{}")


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {}}]}],
)
def test_parse_response_refuses_other_shapes(payload: object) -> None:
    with pytest.raises(LlamaError):
        parse_response(payload)


def test_asking_a_server_that_isnt_running_is_an_error() -> None:
    server = LlamaServer(Path("/nonexistent/llama-server"), Path("/m.gguf"), threads=1)
    with pytest.raises(LlamaError, match="isn't running"):
        server.generate_json("s", "p", schema(1), 10)


def test_a_server_that_exits_at_once_is_reported(tmp_path: Path) -> None:
    fake = tmp_path / "llama-server"
    fake.write_text("#!/bin/sh\necho 'error: model not found' >&2\nexit 1\n")
    fake.chmod(0o755)
    server = LlamaServer(fake, tmp_path / "m.gguf", threads=1, log=tmp_path / "log.txt")
    with pytest.raises(LlamaError, match="model not found"):
        server.start()


def test_judge_downloads_are_pinned_opt_in_and_not_in_the_default_set() -> None:
    from tomecast import manifest

    for asset in assets.JUDGE_ASSETS + (assets.MINICPM5_Q4_K_M,):
        assert asset.opt_in
        assert len(asset.sha256) == 64
        assert asset not in manifest.ASSETS
        assert "/resolve/main/" not in asset.url  # pinned to a commit, never "latest"
    assert assets.LLAMA_SERVER.startswith(f"llama-{assets.LLAMA_CPP_BUILD}/")
