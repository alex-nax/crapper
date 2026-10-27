import shutil
import sys
from pathlib import Path

import pytest

from crapper import cmake_coverage as cc
from crapper.analyze import analyze_files
from crapper.coverage import load_bundle

FIXTURE = Path(__file__).parent / "fixtures" / "cmake_c"


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _project(root: Path) -> Path:
    _write(root, "CMakeLists.txt", "cmake_minimum_required(VERSION 3.17)\nproject(demo C)\n")
    return _write(root, "src/demo.c", "int f(int a) {\n  return a;\n}\n")


# Finding the project ------------------------------------------------------


def test_project_is_the_nearest_cmakelists_that_calls_project(tmp_path):
    _write(tmp_path, "CMakeLists.txt", "PROJECT(top C)\nadd_subdirectory(src)\n")
    _write(tmp_path, "src/CMakeLists.txt", "add_library(x x.c)\n")
    _write(tmp_path, "lib/CMakeLists.txt", "project(lib CXX)\n")
    files = [
        _write(tmp_path, "src/x.c", ""),
        _write(tmp_path, "lib/y.cpp", ""),
        _write(tmp_path, "tool.py", ""),
    ]
    assert cc.cmake_projects(tmp_path, files) == [tmp_path.resolve(), (tmp_path / "lib").resolve()]


def test_no_cmake_project_warns_and_runs_nothing(tmp_path, monkeypatch, capsys):
    _write(tmp_path, "src/CMakeLists.txt", "add_library(x x.c)\n")
    source = _write(tmp_path, "src/x.c", "int f(void) { return 0; }\n")
    calls = []
    monkeypatch.setattr(cc, "run", lambda *args, **kw: calls.append(args) or 0)
    cc.cover_c(tmp_path, [source])
    assert calls == []
    assert not (tmp_path / "target" / "crapper-build").exists()
    assert "No CMakeLists.txt with project(); skipping C and C++ coverage" in capsys.readouterr().err


def test_build_and_report_live_under_target_never_the_users_build(tmp_path):
    root = tmp_path.resolve()
    assert cc.build_dir(root, root) == root / "target" / "crapper-build" / "c" / "_root"
    assert cc.build_dir(root, root / "a" / "b") == root / "target" / "crapper-build" / "c" / "a__b"
    assert cc.report_path(root, root) == root / "target" / "coverage" / "c" / "lcov.info"
    assert cc.report_path(root, root / "a") == root / "target" / "coverage" / "c" / "a" / "lcov.info"


# Commands -----------------------------------------------------------------


def test_configure_instruments_each_language_and_linker(tmp_path):
    build = tmp_path / "b"
    plain = cc.configure_command(tmp_path, build)
    assert plain == ["cmake", "-S", str(tmp_path), "-B", str(build), "-DCMAKE_BUILD_TYPE=Debug"]
    llvm = cc.configure_command(tmp_path, build, "llvm")
    assert "-DCMAKE_C_FLAGS=-O0 -g -fprofile-instr-generate -fcoverage-mapping" in llvm
    assert "-DCMAKE_CXX_FLAGS=-O0 -g -fprofile-instr-generate -fcoverage-mapping" in llvm
    for kind in ("EXE", "SHARED", "MODULE"):
        assert f"-DCMAKE_{kind}_LINKER_FLAGS=-fprofile-instr-generate" in llvm
    gcov = cc.configure_command(tmp_path, build, "gcov")
    assert "-DCMAKE_C_FLAGS=-O0 -g --coverage" in gcov
    assert "-DCMAKE_EXE_LINKER_FLAGS=--coverage" in gcov


def test_ctest_fails_when_no_tests_are_registered():
    assert "--no-tests=error" in cc.ctest_command()


def test_llvm_profiles_go_into_the_build_dir(tmp_path):
    assert cc.profile_env(tmp_path, "llvm") == {
        "LLVM_PROFILE_FILE": str(tmp_path / "profraw" / "%p-%m.profraw")
    }
    assert cc.profile_env(tmp_path, "gcov") is None


def test_llvm_export_names_every_instrumented_binary(tmp_path):
    tools = cc.Toolchain("llvm", llvm_profdata="/t/llvm-profdata", llvm_cov="/t/llvm-cov")
    merge, export = cc.llvm_commands(
        tools, [tmp_path / "1.profraw"], tmp_path / "c.profdata", [tmp_path / "t1", tmp_path / "lib.so"]
    )
    assert merge == ["/t/llvm-profdata", "merge", "-sparse", "-o", str(tmp_path / "c.profdata"), str(tmp_path / "1.profraw")]
    assert export == [
        "/t/llvm-cov", "export", "-format=lcov", f"-instr-profile={tmp_path / 'c.profdata'}",
        str(tmp_path / "t1"), "-object", str(tmp_path / "lib.so"),
    ]


def test_gcov_readers(tmp_path):
    gcovr = cc.Toolchain("gcov", gcov=["/t/llvm-cov", "gcov"], reader="gcovr")
    command = cc.gcov_reader_command(gcovr, tmp_path, tmp_path / "b", tmp_path / "r")
    assert command[:5] == ["gcovr", "--root", str(tmp_path), "--gcov-executable", "/t/llvm-cov gcov"]
    lcov = cc.Toolchain("gcov", gcov=["/t/gcov-13"], reader="lcov")
    command = cc.gcov_reader_command(lcov, tmp_path, tmp_path / "b", tmp_path / "r")
    assert command[-2:] == ["--gcov-tool", "/t/gcov-13"]
    plain = cc.Toolchain("gcov", gcov=["/t/gcov"])
    assert cc.gcov_command(plain, tmp_path, [tmp_path / "a.c.gcda"]) == [
        "/t/gcov", "-p", "-o", str(tmp_path), str(tmp_path / "a.c.gcda")
    ]


# Toolchain ----------------------------------------------------------------


def _cmake_compiler(build: Path, ident: str, compiler: str) -> None:
    _write(
        build,
        "CMakeFiles/3.31.2/CMakeCCompiler.cmake",
        f'set(CMAKE_C_COMPILER "{compiler}")\nset(CMAKE_C_COMPILER_ID "{ident}")\n',
    )


def test_compiler_comes_from_cmakes_own_detection(tmp_path):
    _cmake_compiler(tmp_path, "AppleClang", "/usr/bin/cc")
    assert cc.compiler_of(tmp_path) == ("AppleClang", "/usr/bin/cc")
    assert cc.compiler_of(tmp_path / "none") == (None, None)


def _tools(monkeypatch, available):
    monkeypatch.setattr(cc, "find_tool", lambda name, _compiler: available.get(name))
    monkeypatch.setattr(cc.shutil, "which", lambda name: None)


def test_clang_prefers_source_based_coverage(monkeypatch):
    _tools(monkeypatch, {"llvm-profdata": "/x/llvm-profdata", "llvm-cov": "/x/llvm-cov"})
    tools = cc.choose_toolchain("Clang", "/x/clang")
    assert (tools.backend, tools.llvm_profdata, tools.llvm_cov) == ("llvm", "/x/llvm-profdata", "/x/llvm-cov")


def test_clang_without_profdata_reads_gcov_through_llvm_cov(monkeypatch):
    _tools(monkeypatch, {"llvm-cov": "/x/llvm-cov"})
    tools = cc.choose_toolchain("AppleClang", "/x/clang")
    assert (tools.backend, tools.gcov, tools.reader) == ("gcov", ["/x/llvm-cov", "gcov"], "gcov")


def test_lcov_never_gets_a_two_word_gcov_tool(monkeypatch):
    monkeypatch.setattr(cc, "find_tool", lambda name, _c: {"llvm-cov": "/x/llvm-cov"}.get(name))
    monkeypatch.setattr(cc.shutil, "which", lambda name: "/x/lcov" if name == "lcov" else None)
    tools = cc.choose_toolchain("Clang", "/x/clang")
    assert (tools.gcov, tools.reader) == (["/x/llvm-cov", "gcov"], "gcov")
    monkeypatch.setattr(cc, "find_tool", lambda name, _c: {"gcov": "/x/gcov"}.get(name))
    assert cc.choose_toolchain("GNU", "/x/gcc").reader == "lcov"


def test_c_files_outside_every_project_are_named(tmp_path, capsys):
    _write(tmp_path, "app/CMakeLists.txt", "project(app C)\n")
    files = [_write(tmp_path, "app/a.c", ""), _write(tmp_path, "stray/b.c", "")]
    assert cc.cmake_projects(tmp_path, files) == [(tmp_path / "app").resolve()]
    assert "No CMake project owns b.c; they score 0%." in capsys.readouterr().err


def test_gcc_uses_its_versioned_gcov(monkeypatch):
    _tools(monkeypatch, {"gcov-13": "/x/gcov-13", "gcov": "/x/gcov"})
    assert cc.choose_toolchain("GNU", "/x/gcc-13").gcov == ["/x/gcov-13"]
    assert cc.choose_toolchain("GNU", "/x/gcc").gcov == ["/x/gcov"]


def test_missing_tools_or_unknown_compiler_give_no_toolchain(monkeypatch, capsys):
    _tools(monkeypatch, {})
    assert cc.choose_toolchain("GNU", "/x/gcc") is None
    assert cc.choose_toolchain("MSVC", "cl.exe") is None
    err = capsys.readouterr().err
    assert "No gcov or llvm-cov was found." in err
    assert "No coverage support for the MSVC compiler." in err


# Reading gcov text and joining it ------------------------------------------

_GCOV = """\
        -:    0:Source:{source}
        -:    0:Graph:x.gcno
        -:    1:#include "u.h"
        3:    2:int used(int a) {{
       3*:    3:  if (a) return 1;
    #####:    4:  return 0;
    =====:    5:  throw;
        -:    6:}}
"""


def test_gcov_counts_never_run_lines_as_zero_and_skips_non_code(tmp_path):
    source, lines = cc.parse_gcov(_GCOV.format(source="src/u.c"), tmp_path)
    assert source == str((tmp_path / "src" / "u.c").resolve())
    assert lines == {2: 3, 3: 3, 4: 0, 5: 0}


def test_gcov_merges_a_header_seen_by_two_units_and_drops_outside_files():
    records = [("/p/u.h", {1: 0, 2: 1}), ("/p/u.h", {1: 2}), ("/usr/include/stdio.h", {1: 1})]
    merged = cc.merge_gcov(records, lambda source: source.startswith("/p/"))
    assert merged == {"/p/u.h": {1: 2, 2: 1}}
    assert cc.to_lcov(merged) == "SF:/p/u.h\nDA:1,2\nDA:2,1\nend_of_record\n"


def test_gcov_coverage_joins_to_the_right_one_of_two_same_named_files(tmp_path):
    body = "int used(int a) {\n  if (a) return 1;\n  return 0;\n}\n"
    a = _write(tmp_path, "a/util.c", body)
    b = _write(tmp_path, "b/util.c", body)
    ran = "        -:    0:Source:{s}\n        1:    1:x\n        1:    2:x\n        1:    3:x\n"
    idle = "        -:    0:Source:{s}\n    #####:    1:x\n    #####:    2:x\n    #####:    3:x\n"
    records = [
        cc.parse_gcov(ran.format(s="a/util.c"), tmp_path),
        cc.parse_gcov(idle.format(s="b/util.c"), tmp_path),
    ]
    report = _write(tmp_path, "target/coverage/c/lcov.info", cc.to_lcov(cc.merge_gcov(records, lambda _s: True)))
    assert report.read_text().count("SF:") == 2
    entries = analyze_files([a, b], tmp_path, load_bundle(tmp_path))
    found = {(e.namespace, e.coverage) for e in entries}
    assert found == {("a/util", 100.0), ("b/util", 0.0)}


def test_report_paths_are_resolved_like_the_sources(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real", target_is_directory=True)
    text = cc.resolve_sources(f"SF:{link}/a.c\nDA:1,1\nend_of_record\n")
    assert text.splitlines()[0] == f"SF:{(tmp_path / 'real' / 'a.c').resolve()}"


def test_a_report_without_lines_is_not_coverage():
    assert not cc.has_line_data("SF:/p/a.c\nend_of_record\n")
    assert cc.has_line_data("SF:/p/a.c\nDA:1,0\nend_of_record\n")


# The run, with the tools faked ---------------------------------------------


class FakeTools:
    """Plays cmake, ctest and the llvm tools; `ctest_writes` decides what the tests leave."""

    def __init__(self, monkeypatch, root, ctest_code=0, ctest_writes=True):
        self.commands = []
        self.ctest_code = ctest_code
        self.ctest_writes = ctest_writes
        self.build = cc.build_dir(root.resolve(), root.resolve())
        monkeypatch.setattr(cc, "run", self.run)
        monkeypatch.setattr(cc, "capture", self.capture)
        monkeypatch.setattr(cc.shutil, "which", lambda name: f"/x/{name}")
        monkeypatch.setattr(cc, "find_tool", lambda name, _c: f"/x/{name}")
        monkeypatch.setattr(cc, "instrumented_binaries", lambda build: [build / "calc_test"])
        self.source = (root / "src" / "demo.c").resolve()

    def run(self, command, cwd, env=None):
        self.commands.append(command)
        if command[:2] == ["cmake", "-S"]:
            _cmake_compiler(self.build, "Clang", "/x/clang")
        if command[0] == "ctest":
            if self.ctest_writes:
                _write(Path(env["LLVM_PROFILE_FILE"]).parent, "1.profraw", "raw")
            return self.ctest_code
        return 0

    def capture(self, command, cwd):
        self.commands.append(command)
        return 0, f"SF:{self.source}\nDA:1,1\nDA:2,1\nDA:3,1\nend_of_record\n"


def _stale_report(root: Path) -> Path:
    return _write(root, "target/coverage/c/lcov.info", f"SF:{root / 'src/demo.c'}\nDA:1,9\nDA:2,9\nDA:3,9\nend_of_record\n")


def test_fake_run_writes_a_report_that_scores(tmp_path, monkeypatch):
    source = _project(tmp_path)
    fake = FakeTools(monkeypatch, tmp_path)
    cc.cover_c(tmp_path, [source])
    assert [c[0] for c in fake.commands] == ["cmake", "cmake", "cmake", "ctest", "/x/llvm-profdata", "/x/llvm-cov"]
    entries = analyze_files([source], tmp_path, load_bundle(tmp_path))
    assert [(e.name, e.coverage) for e in entries] == [("f", 100.0)]


def test_failed_tests_write_no_report(tmp_path, monkeypatch, capsys):
    source = _project(tmp_path)
    _stale_report(tmp_path)
    FakeTools(monkeypatch, tmp_path, ctest_code=3)
    cc.cover_c(tmp_path, [source])
    assert not (tmp_path / "target" / "coverage" / "c" / "lcov.info").exists()
    assert "ctest exited 3" in capsys.readouterr().err
    entries = analyze_files([source], tmp_path, load_bundle(tmp_path))
    assert [(e.name, e.coverage) for e in entries] == [("f", 0.0)]


def test_tests_that_record_no_counters_are_an_uninstrumented_build(tmp_path, monkeypatch, capsys):
    source = _project(tmp_path)
    _stale_report(tmp_path)
    fake = FakeTools(monkeypatch, tmp_path, ctest_writes=False)
    cc.cover_c(tmp_path, [source])
    assert not (tmp_path / "target" / "coverage" / "c" / "lcov.info").exists()
    assert "wrote no .profraw; the build was not instrumented" in capsys.readouterr().err
    assert "/x/llvm-cov" not in [c[0] for c in fake.commands]
    entries = analyze_files([source], tmp_path, load_bundle(tmp_path))
    assert [(e.name, e.coverage) for e in entries] == [("f", 0.0)]


def test_gcov_build_without_gcda_is_uninstrumented(tmp_path, capsys):
    tools = cc.Toolchain("gcov", gcov=["/x/gcov"])
    assert cc._export_gcov(tools, tmp_path, tmp_path / "b", tmp_path / "r") is None
    assert "wrote no .gcda counters" in capsys.readouterr().err


def test_failed_configure_stops_before_tests(tmp_path, monkeypatch, capsys):
    source = _project(tmp_path)
    commands = []
    monkeypatch.setattr(cc.shutil, "which", lambda name: f"/x/{name}")
    monkeypatch.setattr(cc, "run", lambda command, cwd, env=None: commands.append(command) or 1)
    cc.cover_c(tmp_path, [source])
    assert [c[0] for c in commands] == ["cmake"]
    assert "CMake could not configure" in capsys.readouterr().err


def test_missing_cmake_scores_zero_with_a_warning(tmp_path, monkeypatch, capsys):
    source = _project(tmp_path)
    monkeypatch.setattr(cc.shutil, "which", lambda name: None)
    monkeypatch.setattr(cc, "run", lambda *a, **k: pytest.fail("ran a command"))
    cc.cover_c(tmp_path, [source])
    assert "cmake or ctest is not installed" in capsys.readouterr().err


# A real build ---------------------------------------------------------------


def _real_skip(backend: str) -> str | None:
    if not shutil.which("cmake") or not shutil.which("ctest"):
        return "cmake/ctest not installed"
    if not (shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")):
        return "no C compiler on PATH"
    if backend == "llvm" and not cc.find_tool("llvm-profdata", shutil.which("clang")):
        return "llvm-profdata not found (clang source-based coverage unavailable)"
    if backend == "gcov" and not (cc.find_tool("gcov", None) or cc.find_tool("llvm-cov", None)):
        return "neither gcov nor llvm-cov found"
    return None


@pytest.mark.parametrize("backend", ["llvm", "gcov"])
def test_real_cmake_project_scores_the_covered_function_higher(tmp_path, backend):
    reason = _real_skip(backend)
    if reason:
        pytest.skip(reason)
    root = tmp_path / "proj"
    shutil.copytree(FIXTURE, root)
    source = root / "src" / "calc.c"
    cc.cover_c(root, [source], prefer=backend)
    report = root / "target" / "coverage" / "c" / "lcov.info"
    if not report.is_file():
        compiler_id, _ = cc.compiler_of(cc.build_dir(root.resolve(), root.resolve()))
        if backend == "llvm" and compiler_id not in ("Clang", "AppleClang"):
            pytest.skip(f"the default compiler is {compiler_id}, not clang")
    assert report.is_file()
    entries = {e.name: e.coverage for e in analyze_files([source], root, load_bundle(root))}
    assert entries["uncovered"] == 0.0
    assert entries["covered"] > entries["uncovered"]
    assert entries["covered"] == 100.0
