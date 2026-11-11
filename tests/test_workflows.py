"""The Integration workflow (a real conversion on seven distros) runs only for the files in its
path filter, so that filter must cover everything ``tomecast convert`` runs. These tests check
it against what the command line actually imports, so a new module can't be missed."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
INTEGRATION = ROOT / ".github" / "workflows" / "integration.yml"


def filter_patterns() -> list[str]:
    """The ``paths: &paths`` list in integration.yml, in order. (It's a plain YAML list, so
    there's no need for a YAML library.)"""
    lines = INTEGRATION.read_text(encoding="utf-8").splitlines()
    start = lines.index("    paths: &paths") + 1
    patterns: list[str] = []
    for line in lines[start:]:
        item = line.strip()
        if item.startswith("#"):
            continue
        if not item.startswith("- "):
            break
        patterns.append(item[2:].strip().strip("\"'"))
    return patterns


def matches(pattern: str, path: str) -> bool:
    """GitHub's filter patterns: ``**`` matches anything, ``*`` anything but a ``/``."""
    regex = "".join(
        ".*" if part == "**" else "[^/]*" if part == "*" else re.escape(part)
        for part in re.split(r"(\*\*|\*)", pattern)
    )
    return re.fullmatch(regex, path) is not None


def runs_for(path: str, patterns: list[str]) -> bool:
    """Whether a change to ``path`` starts the workflow. As on GitHub, the last pattern that
    matches decides, and one starting with ``!`` leaves the path out."""
    included = False
    for pattern in patterns:
        if pattern.startswith("!"):
            included = included and not matches(pattern[1:], path)
        elif matches(pattern, path):
            included = True
    return included


def module_file(name: str) -> Path | None:
    base = SRC.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def modules_the_command_line_runs() -> set[Path]:
    """Every Tomecast module ``tomecast.cli`` imports, directly or not, with the packages
    they're in. The window (``tomecast.gui``) is left out: only ``tomecast gui`` loads it."""
    seen: set[Path] = set()
    queue = ["tomecast.cli"]
    while queue:
        name = queue.pop()
        if name == "tomecast.gui" or name.startswith("tomecast.gui."):
            continue
        parts = name.split(".")
        for end in range(1, len(parts) + 1):  # a module's packages are imported first
            module = ".".join(parts[:end])
            path = module_file(module)
            if path is None or path in seen:
                continue
            seen.add(path)
            package = module if path.name == "__init__.py" else module.rpartition(".")[0]
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    queue += [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    target = node.module or ""
                    if node.level:  # relative: from the module's own package upwards
                        base = package.split(".")[: len(package.split(".")) - node.level + 1]
                        target = ".".join([*base, target] if target else base)
                    queue.append(target)
                    queue += [f"{target}.{alias.name}" for alias in node.names]  # submodules
    return {path for path in seen if path.is_relative_to(SRC / "tomecast")}


def test_a_real_conversion_runs_for_everything_the_command_line_runs() -> None:
    patterns = filter_patterns()
    modules = modules_the_command_line_runs()
    names = {path.relative_to(SRC).as_posix() for path in modules}
    # The walk found the command line's pieces (the convert path, not just the engine).
    assert {"tomecast/cli.py", "tomecast/machine.py", "tomecast/chunking.py"} <= names
    assert "tomecast/readers/epub.py" in names

    missed = sorted(
        path.relative_to(ROOT).as_posix()
        for path in modules
        if not runs_for(path.relative_to(ROOT).as_posix(), patterns)
    )

    assert missed == []


def test_a_real_conversion_runs_for_the_installer_the_dependencies_and_the_checks() -> None:
    patterns = filter_patterns()
    workflow = INTEGRATION.read_text(encoding="utf-8")
    scripts = set(re.findall(r"\.github/scripts/[\w.-]+\.py", workflow))
    assert scripts  # the scripts the jobs run

    for path in ["install.sh", "pyproject.toml", "uv.lock", ".github/workflows/integration.yml"]:
        assert runs_for(path, patterns), path
    for script in sorted(scripts):
        assert runs_for(script, patterns), script
    assert "    paths: *paths" in workflow  # pushes to main use the same list


def test_the_windows_own_code_and_the_docs_dont_start_the_slow_job() -> None:
    patterns = filter_patterns()

    assert not runs_for("src/tomecast/gui/worker.py", patterns)
    assert not runs_for("src/tomecast/gui/qml/Main.qml", patterns)
    assert not runs_for("README.md", patterns)
    assert not runs_for("docs/architecture.md", patterns)
    assert not runs_for("tests/test_cli.py", patterns)
