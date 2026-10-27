from pathlib import Path

import pytest

from crapper.analyze import analyze_files
from crapper.coverage import CoverageBundle
from crapper.discover import language_of
from crapper.languages import functions_in_file
from crapper.runners import run_coverage


def _scored(language, source, path="src/demo.cpp"):
    return [
        (fn.namespace, fn.name, fn.complexity)
        for fn in functions_in_file(language, source, path, "/proj")
    ]


def _complexity(language, body):
    source = f"int f(int a, int b) {{ {body} }}\n"
    found = functions_in_file(language, source, "src/demo.c", "/proj")
    assert [fn.name for fn in found] == ["f"]
    return found[0].complexity


_EACH_DECISION = [
    ("if", "if (a) return 1; return 0;"),
    ("for", "for (int i = 0; i < a; i++) b++; return b;"),
    ("while", "while (a) a--; return a;"),
    ("do", "do { a--; } while (a); return a;"),
    ("case", "switch (a) { case 1: return 1; } return 0;"),
    ("ternary", "return a ? 1 : 0;"),
    ("and", "return a && b;"),
    ("or", "return a || b;"),
]


@pytest.mark.parametrize("language", ["c", "cpp"])
@pytest.mark.parametrize(("kind", "body"), _EACH_DECISION, ids=[k for k, _ in _EACH_DECISION])
def test_each_decision_adds_one(language, kind, body):
    assert _complexity(language, body) == 2


def test_range_for_adds_one():
    assert _complexity("cpp", "for (int x : v) b += x; return b;") == 2


def test_catch_adds_one_per_handler():
    body = "try { g(); } catch (int) { return 1; } catch (...) { return 2; } return 0;"
    assert _complexity("cpp", body) == 3


def test_alternative_tokens_count_like_symbols():
    assert _complexity("cpp", "return a and b or a;") == 3


def test_default_is_not_a_decision():
    body = "switch (a) { case 1: return 1; case 2: return 2; default: return 0; }"
    assert _complexity("c", body) == 3


def test_each_logical_operator_counts_once():
    assert _complexity("c", "return a && b && a;") == 3


def test_else_if_is_one_decision_each():
    assert _complexity("c", "if (a) return 1; else if (b) return 2; else return 3;") == 3


def test_preprocessor_branches_are_scored_as_written():
    source = (
        "int f(int a) {\n#ifdef FAST\n  if (a) return 1;\n#else\n  return 2;\n"
        "#endif\n  return 0;\n}\n"
    )
    assert _scored("c", source, "src/demo.c") == [("src/demo", "f", 2)]


def test_a_lambda_is_its_own_entry_and_not_its_parents():
    source = (
        "int run(int a) {\n"
        "  auto pick = [&](int z) { if (z) return 1; return z && a; };\n"
        "  return pick(a) ? 1 : 0;\n"
        "}\n"
    )
    assert _scored("cpp", source) == [
        ("src/demo", "run", 2),
        ("src/demo", "run::lambda@2", 3),
    ]


def test_a_local_class_method_is_not_charged_to_its_function():
    source = "int run() {\n  struct L { int g(int x) { return x ? 1 : 0; } };\n  return 0;\n}\n"
    assert _scored("cpp", source) == [("src/demo", "run", 1), ("L", "g", 2)]


def test_declarations_without_bodies_are_not_functions():
    source = (
        "int proto(int);\n"
        "struct S {\n  S() = default;\n  void gone() = delete;\n  int later(int);\n"
        "  int now() { return 1; }\n};\n"
    )
    assert _scored("cpp", source) == [("S", "now", 1)]


def test_a_function_try_block_is_a_function():
    source = "int g() try { return h(); } catch (...) { return 0; }\n"
    assert _scored("cpp", source) == [("src/demo", "g", 2)]


def test_cpp_namespaces_classes_and_out_of_line_definitions():
    source = (
        "namespace game { namespace board {\n"
        "class Widget {\n public:\n  int inline_get() const { return 1; }\n  ~Widget();\n};\n"
        "int Widget::get(int a) { return a; }\n"
        "Widget::~Widget() {}\n"
        "}}\n"
        "namespace a::b { int nested() { return 0; } }\n"
        "template <typename T> int Box<T>::size() const { return 0; }\n"
        "void game::free_fn() {}\n"
        "namespace { int hidden() { return 0; } }\n"
        'extern "C" { int exported() { return 0; } }\n'
        "bool operator==(const Widget&, const Widget&) { return true; }\n"
    )
    assert _scored("cpp", source, "src/game/widget.cpp") == [
        ("game::board::Widget", "inline_get", 1),
        ("game::board::Widget", "get", 1),
        ("game::board::Widget", "~Widget", 1),
        ("a::b", "nested", 1),
        ("Box", "size", 1),
        ("game", "free_fn", 1),
        ("src/game/widget", "hidden", 1),
        ("src/game/widget", "exported", 1),
        ("src/game/widget", "operator==", 1),
    ]


def test_overloads_in_one_file_keep_their_parameter_types():
    source = (
        "namespace m {\n"
        "int add(int a, int b) { return a + b; }\n"
        "double add(const double& a, double b = 1.0) { return a; }\n"
        "int sub(int a) { return -a; }\n"
        "}\n"
    )
    assert _scored("cpp", source) == [
        ("m", "add(int, int)", 1),
        ("m", "add(const double&, double)", 1),
        ("m", "sub", 1),
    ]


def test_c_functions_are_namespaced_by_file_path():
    source = (
        "static const char *name(int x) { return x ? \"a\" : \"b\"; }\n"
        "int (*handler(void))(int) { return 0; }\n"
    )
    found = functions_in_file("c", source, "src/render/gl.c", "/proj")
    assert [(fn.namespace, fn.name, fn.language) for fn in found] == [
        ("src/render/gl", "name", "c"),
        ("src/render/gl", "handler", "c"),
    ]
    assert found[0].start_byte == 0
    assert found[0].end_byte == source.index("\n")


def test_extensions_map_to_c_and_cpp():
    assert [language_of(f"x{ext}") for ext in (".c", ".h")] == ["c", "c"]
    cpp = [".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"]
    assert [language_of(f"x{ext}") for ext in cpp] == ["cpp"] * len(cpp)


def test_c_without_a_cmake_project_is_announced_as_skipped(tmp_path, capsys):
    source = tmp_path / "src" / "demo.c"
    source.parent.mkdir()
    source.write_text("int f(void) { return 0; }\n", encoding="utf-8")
    run_coverage(tmp_path, [source], None)
    assert "No CMakeLists.txt with project(); skipping C and C++ coverage" in capsys.readouterr().err


def test_c_without_coverage_scores_zero(tmp_path):
    source = tmp_path / "src" / "demo.cpp"
    source.parent.mkdir()
    source.write_text("int f(int a) { return a ? 1 : 0; }\n", encoding="utf-8")
    entries = analyze_files([source], tmp_path, CoverageBundle())
    assert [(e.name, e.coverage, e.crap) for e in entries] == [("f", 0.0, 6.0)]


def test_c_reads_an_lcov_report(tmp_path):
    source = tmp_path / "src" / "demo.c"
    source.parent.mkdir()
    source.write_text("int f(int a) {\n  return a;\n}\n", encoding="utf-8")
    report = tmp_path / "target" / "coverage" / "c" / "lcov.info"
    report.parent.mkdir(parents=True)
    report.write_text("SF:src/demo.c\nDA:1,1\nDA:2,1\nDA:3,1\nend_of_record\n", encoding="utf-8")
    from crapper.coverage import load_bundle

    entries = analyze_files([source], tmp_path, load_bundle(Path(tmp_path)))
    assert [(e.name, e.coverage) for e in entries] == [("f", 100.0)]
