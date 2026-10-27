"""Collect C and C++ coverage from a CMake project and write it as LCOV.

Each CMake project (the nearest `CMakeLists.txt` above a source that calls
`project(`) is configured and built instrumented in its own directory under
`target/crapper-build/`, never the project's own build tree. Its tests run
through `ctest`, and the counters are exported as LCOV under
`target/coverage/c/`, where the loader joins them to functions by file and
line range.

Clang uses source-based coverage (`llvm-profdata` and `llvm-cov export`) when
both tools are found; otherwise, and for GCC, the build uses `--coverage` and
the `.gcda` counters are read with gcovr, lcov, or `gcov` itself.

A missing tool, a failed step, or a build that recorded no counters writes no
report, so C and C++ score 0% with a warning.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_PROJECT = re.compile(r"^\s*project\s*\(", re.IGNORECASE | re.MULTILINE)
_COMPILER_ID = re.compile(r'set\(CMAKE_(?:C|CXX)_COMPILER_ID "([^"]*)"\)')
_COMPILER = re.compile(r'set\(CMAKE_(?:C|CXX)_COMPILER "([^"]*)"\)')
_GCOV_LINE = re.compile(r"^\s*([^:]+):\s*(\d+):(.*)$")
_LANGUAGES = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"}

_FLAGS = {
    "llvm": ("-O0 -g -fprofile-instr-generate -fcoverage-mapping", "-fprofile-instr-generate"),
    "gcov": ("-O0 -g --coverage", "--coverage"),
}


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def _show(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> int:
    _warn(f"+ ({cwd}) {_show(command)}")
    merged = None if env is None else {**os.environ, **env}
    try:
        return subprocess.run(command, cwd=cwd, env=merged).returncode
    except OSError as exc:
        _warn(f"Coverage command failed to start: {exc}")
        return 127


def capture(command: list[str], cwd: Path) -> tuple[int, str]:
    _warn(f"+ ({cwd}) {_show(command)}")
    try:
        done = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        _warn(f"Coverage command failed to start: {exc}")
        return 127, ""
    if done.returncode != 0 and done.stderr:
        _warn(done.stderr.strip())
    return done.returncode, done.stdout


@dataclass
class Toolchain:
    """How one build is instrumented and read back."""

    backend: str
    llvm_profdata: str | None = None
    llvm_cov: str | None = None
    gcov: list[str] | None = None
    reader: str = "gcov"


def _has_project(directory: Path) -> bool:
    path = directory / "CMakeLists.txt"
    if not path.is_file():
        return False
    return bool(_PROJECT.search(path.read_text(encoding="utf-8", errors="replace")))


def _project_of(file: Path, root: Path) -> Path | None:
    current = file.parent
    while True:
        if _has_project(current):
            return current
        if current == root or current.parent == current:
            return None
        current = current.parent


def cmake_projects(root: Path, files: list[Path]) -> list[Path]:
    """The CMake projects that own the C and C++ files, one per `project(`."""

    root = root.resolve()
    found: set[Path] = set()
    for file in files:
        if Path(file).suffix not in _LANGUAGES:
            continue
        project = _project_of(Path(file).resolve(), root)
        if project is not None:
            found.add(project)
    return sorted(found)


def _slug(root: Path, module: Path) -> str:
    if module == root:
        return ""
    return module.relative_to(root).as_posix().replace("/", "__")


def build_dir(root: Path, module: Path) -> Path:
    base = root / "target" / "crapper-build" / "c"
    slug = _slug(root, module)
    return base / slug if slug else base / "_root"


def report_path(root: Path, module: Path) -> Path:
    base = root / "target" / "coverage" / "c"
    slug = _slug(root, module)
    return base / slug / "lcov.info" if slug else base / "lcov.info"


def configure_command(module: Path, build: Path, backend: str | None = None) -> list[str]:
    command = ["cmake", "-S", str(module), "-B", str(build), "-DCMAKE_BUILD_TYPE=Debug"]
    if backend is None:
        return command
    compile_flags, link_flags = _FLAGS[backend]
    for language in ("C", "CXX"):
        command.append(f"-DCMAKE_{language}_FLAGS={compile_flags}")
    for kind in ("EXE", "SHARED", "MODULE"):
        command.append(f"-DCMAKE_{kind}_LINKER_FLAGS={link_flags}")
    return command


def build_command(build: Path) -> list[str]:
    return ["cmake", "--build", str(build), "--parallel"]


def ctest_command() -> list[str]:
    return ["ctest", "--output-on-failure", "--no-tests=error", "-C", "Debug"]


def profile_env(build: Path, backend: str) -> dict[str, str] | None:
    if backend != "llvm":
        return None
    return {"LLVM_PROFILE_FILE": str(build / "profraw" / "%p-%m.profraw")}


def compiler_of(build: Path) -> tuple[str | None, str | None]:
    """The compiler id (`AppleClang`, `Clang`, `GNU`) and path CMake chose."""

    for name in ("CMakeCCompiler.cmake", "CMakeCXXCompiler.cmake"):
        for path in sorted((build / "CMakeFiles").glob(f"*/{name}")):
            text = path.read_text(encoding="utf-8", errors="replace")
            ident = _COMPILER_ID.search(text)
            if ident and ident.group(1):
                compiler = _COMPILER.search(text)
                return ident.group(1), compiler.group(1) if compiler else None
    return None, None


def find_tool(name: str, compiler: str | None) -> str | None:
    """A tool beside the compiler, then on PATH, then from `xcrun` on macOS."""

    if compiler:
        sibling = Path(compiler).resolve().parent / name
        if sibling.is_file() and os.access(sibling, os.X_OK):
            return str(sibling)
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "darwin" and shutil.which("xcrun"):
        code, out = capture(["xcrun", "--find", name], Path.cwd())
        if code == 0 and out.strip():
            return out.strip()
    return None


def _gcc_gcov(compiler: str | None) -> list[str] | None:
    if compiler:
        match = re.search(r"g(?:cc|\+\+)(-\d+)$", Path(compiler).name)
        if match:
            versioned = find_tool(f"gcov{match.group(1)}", compiler)
            if versioned:
                return [versioned]
    found = find_tool("gcov", compiler)
    return [found] if found else None


def _clang_gcov(compiler: str | None) -> list[str] | None:
    llvm_cov = find_tool("llvm-cov", compiler)
    if llvm_cov:
        return [llvm_cov, "gcov"]
    found = find_tool("gcov", compiler)
    return [found] if found else None


def _gcov_reader() -> str:
    if shutil.which("gcovr"):
        return "gcovr"
    if shutil.which("lcov"):
        return "lcov"
    return "gcov"


def choose_toolchain(
    compiler_id: str | None, compiler: str | None, prefer: str | None = None
) -> Toolchain | None:
    """Source-based coverage for Clang when its tools exist, else gcov counters."""

    if compiler_id in ("Clang", "AppleClang"):
        if prefer != "gcov":
            profdata = find_tool("llvm-profdata", compiler)
            llvm_cov = find_tool("llvm-cov", compiler)
            if profdata and llvm_cov:
                return Toolchain("llvm", llvm_profdata=profdata, llvm_cov=llvm_cov)
        gcov = _clang_gcov(compiler)
    elif compiler_id == "GNU":
        gcov = _gcc_gcov(compiler)
    else:
        _warn(f"No coverage support for the {compiler_id or 'unknown'} compiler.")
        return None
    if gcov is None:
        _warn("No gcov or llvm-cov was found.")
        return None
    return Toolchain("gcov", gcov=gcov, reader=_gcov_reader())


def _files_under(build: Path, suffix: str) -> list[Path]:
    return sorted(build.rglob(f"*{suffix}"))


def clear_counters(build: Path) -> None:
    """Counters from an earlier run would add to this one."""

    for gcda in _files_under(build, ".gcda"):
        gcda.unlink()
    shutil.rmtree(build / "profraw", ignore_errors=True)


def _is_binary_candidate(path: Path) -> bool:
    if not path.is_file() or "CMakeFiles" in path.parts:
        return False
    if path.suffix in (".dylib", ".so", ".dll", ".exe"):
        return True
    return os.access(path, os.X_OK) and path.suffix == ""


def instrumented_binaries(build: Path) -> list[Path]:
    """Executables and libraries that carry an LLVM coverage map."""

    found = []
    for path in sorted(build.rglob("*")):
        if not _is_binary_candidate(path):
            continue
        try:
            if b"__llvm_covmap" in path.read_bytes():
                found.append(path)
        except OSError:
            continue
    return found


def llvm_commands(
    tools: Toolchain, profraws: list[Path], profdata: Path, binaries: list[Path]
) -> tuple[list[str], list[str]]:
    merge = [tools.llvm_profdata, "merge", "-sparse", "-o", str(profdata)]
    merge.extend(str(path) for path in profraws)
    export = [tools.llvm_cov, "export", "-format=lcov", f"-instr-profile={profdata}"]
    export.append(str(binaries[0]))
    for extra in binaries[1:]:
        export.extend(["-object", str(extra)])
    return merge, export


def gcov_reader_command(tools: Toolchain, module: Path, build: Path, report: Path) -> list[str]:
    if tools.reader == "gcovr":
        return [
            "gcovr", "--root", str(module), "--gcov-executable", " ".join(tools.gcov),
            "--lcov", str(report), str(build),
        ]
    return [
        "lcov", "--capture", "--directory", str(build), "--output-file", str(report),
        "--gcov-tool", tools.gcov[0],
    ]


def gcov_command(tools: Toolchain, object_dir: Path, gcdas: list[Path]) -> list[str]:
    return [*tools.gcov, "-p", "-o", str(object_dir), *(str(path) for path in gcdas)]


def _gcov_hits(count: str) -> int | None:
    count = count.strip().rstrip("*")
    if count == "-":
        return None
    if count.startswith(("#", "=")):
        return 0
    try:
        return int(count)
    except ValueError:
        return 1


def parse_gcov(text: str, base: Path) -> tuple[str | None, dict[int, int]]:
    """One `.gcov` text file as (absolute source, line → hits).

    `#####` and `=====` are executable lines that never ran; `-` is a line
    with no code.
    """

    source = None
    lines: dict[int, int] = {}
    for raw in text.splitlines():
        match = _GCOV_LINE.match(raw)
        if not match:
            continue
        number = int(match.group(2))
        if number == 0:
            if match.group(3).startswith("Source:"):
                source = str((base / match.group(3)[len("Source:"):]).resolve())
            continue
        hits = _gcov_hits(match.group(1))
        if hits is not None:
            lines[number] = lines.get(number, 0) + hits
    return source, lines


def merge_gcov(records: list[tuple[str | None, dict[int, int]]], keep) -> dict[str, dict[int, int]]:
    merged: dict[str, dict[int, int]] = {}
    for source, lines in records:
        if source is None or not keep(source):
            continue
        target = merged.setdefault(source, {})
        for number, hits in lines.items():
            target[number] = target.get(number, 0) + hits
    return merged


def to_lcov(files: dict[str, dict[int, int]]) -> str:
    out = []
    for source in sorted(files):
        out.append(f"SF:{source}")
        for number in sorted(files[source]):
            out.append(f"DA:{number},{files[source][number]}")
        out.append("end_of_record")
    return "\n".join(out) + "\n" if out else ""


def resolve_sources(lcov: str) -> str:
    """Rewrite each absolute `SF:` as its real path, as the loader resolves sources."""

    out = []
    for line in lcov.splitlines():
        if line.startswith("SF:") and Path(line[3:]).is_absolute():
            line = f"SF:{Path(line[3:]).resolve()}"
        out.append(line)
    return "\n".join(out) + "\n"


def has_line_data(lcov: str) -> bool:
    return any(line.startswith("DA:") for line in lcov.splitlines())


def _inside(path: str, module: Path, build: Path) -> bool:
    resolved = Path(path)
    return resolved.is_relative_to(module) and not resolved.is_relative_to(build)


def _run_plain_gcov(tools: Toolchain, module: Path, build: Path) -> str:
    groups: dict[Path, list[Path]] = {}
    for gcda in _files_under(build, ".gcda"):
        groups.setdefault(gcda.parent, []).append(gcda)
    scratch = build / "gcov-out"
    shutil.rmtree(scratch, ignore_errors=True)
    records = []
    for index, (object_dir, gcdas) in enumerate(sorted(groups.items())):
        out = scratch / str(index)
        out.mkdir(parents=True)
        if run(gcov_command(tools, object_dir, gcdas), out) != 0:
            _warn(f"gcov failed in {object_dir}.")
            continue
        for path in sorted(out.glob("*.gcov")):
            records.append(parse_gcov(path.read_text(encoding="utf-8", errors="replace"), build))
    return to_lcov(merge_gcov(records, lambda source: _inside(source, module, build)))


def _export_gcov(tools: Toolchain, module: Path, build: Path, report: Path) -> str | None:
    if not _files_under(build, ".gcda"):
        _warn(f"The tests in {module} wrote no .gcda counters; the build was not instrumented.")
        return None
    if tools.reader == "gcov":
        return _run_plain_gcov(tools, module, build)
    staged = build / "reader.lcov"
    staged.unlink(missing_ok=True)
    if run(gcov_reader_command(tools, module, build, staged), build) != 0 or not staged.is_file():
        _warn(f"{tools.reader} could not read the counters in {build}.")
        return None
    return staged.read_text(encoding="utf-8", errors="replace")


def _export_llvm(tools: Toolchain, module: Path, build: Path) -> str | None:
    profraws = _files_under(build / "profraw", ".profraw")
    if not profraws:
        _warn(f"The tests in {module} wrote no .profraw; the build was not instrumented.")
        return None
    binaries = instrumented_binaries(build)
    if not binaries:
        _warn(f"No instrumented binary was found in {build}.")
        return None
    merge, export = llvm_commands(tools, profraws, build / "coverage.profdata", binaries)
    if run(merge, build) != 0:
        _warn("llvm-profdata merge failed.")
        return None
    code, lcov = capture(export, build)
    if code != 0:
        _warn("llvm-cov export failed.")
        return None
    return lcov


def _build(module: Path, build: Path, prefer: str | None) -> Toolchain | None:
    build.mkdir(parents=True, exist_ok=True)
    if run(configure_command(module, build), build) != 0:
        _warn(f"CMake could not configure {module}.")
        return None
    tools = choose_toolchain(*compiler_of(build), prefer=prefer)
    if tools is None:
        return None
    if run(configure_command(module, build, tools.backend), build) != 0:
        _warn(f"CMake could not configure {module} for coverage.")
        return None
    if run(build_command(build), build) != 0:
        _warn(f"The coverage build of {module} failed.")
        return None
    return tools


def cover_project(root: Path, module: Path, prefer: str | None = None) -> bool:
    """Build, test and export one project. True when a report was written."""

    build = build_dir(root, module)
    report = report_path(root, module)
    tools = _build(module, build, prefer)
    if tools is None:
        return False
    clear_counters(build)
    code = run(ctest_command(), build, profile_env(build, tools.backend))
    if code != 0:
        _warn(f"ctest exited {code} in {module}. C and C++ coverage will score 0%.")
        return False
    if tools.backend == "llvm":
        lcov = _export_llvm(tools, module, build)
    else:
        lcov = _export_gcov(tools, module, build, report)
    if not lcov or not has_line_data(lcov):
        _warn(f"No line coverage was recorded for {module}. C and C++ coverage will score 0%.")
        return False
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(resolve_sources(lcov), encoding="utf-8")
    return True


def cover_c(root: Path, files: list[Path], prefer: str | None = None) -> None:
    """Replace `target/coverage/c/` with fresh reports for every CMake project."""

    root = root.resolve()
    shutil.rmtree(root / "target" / "coverage" / "c", ignore_errors=True)
    projects = cmake_projects(root, files)
    if not projects:
        _warn(
            "No CMakeLists.txt with project(); skipping C and C++ coverage. "
            "Pass --coverage-command to write an LCOV report; without one, C and C++ score 0%."
        )
        return
    if not shutil.which("cmake") or not shutil.which("ctest"):
        _warn("cmake or ctest is not installed. C and C++ coverage will score 0%.")
        return
    for module in projects:
        cover_project(root, module, prefer)
