"""C and C++ functions and cyclomatic complexity.

Decision points are `if`, `for` (and range-`for`), `while`, `do`, each `case`
label (not `default`), `?:`, `&&` / `||` (and `and` / `or`), and `catch`.
Preprocessor conditionals are not decisions and are not expanded: both sides
of an `#if` are scored as written.

Only definitions with a body are functions. Prototypes, member declarations,
and `= default` / `= delete` are not. A lambda is its own entry, named
`<enclosing>::lambda@<line>`, and its decisions are not charged to the
enclosing function. Methods of a local class are likewise their own entries.

C++ functions are namespaced by their namespaces and classes
(`ns::Widget`), including the scope of an out-of-line definition
(`int Widget::get()`). Anonymous namespaces add nothing. Global C++ functions
and every C function are namespaced by the file's project-relative path
without its extension (`src/render/gl`), so a `.c` and its `.h` share one.
When a name repeats in one namespace within a file (overloads), each entry
gets its parameter types appended: `add(int, int)`.
"""

import re
from dataclasses import replace
from pathlib import PurePosixPath

from crapper.languages.treesitter import (
    complexity,
    descendants,
    end_line,
    node_text,
    parse,
    start_line,
)
from crapper.model import Function

_DECISIONS = {
    "if_statement",
    "for_statement",
    "for_range_loop",
    "while_statement",
    "do_statement",
    "conditional_expression",
    "catch_clause",
}
_LOGIC = {"&&", "||", "and", "or"}
_UNITS = {"function_definition", "lambda_expression"}
_BODIES = {"compound_statement", "try_statement"}
_SCOPES = {"class_specifier", "struct_specifier", "union_specifier"}
_WRAPPERS = {
    "pointer_declarator",
    "reference_declarator",
    "parenthesized_declarator",
    "attributed_declarator",
}
_NAMES = {
    "identifier",
    "field_identifier",
    "destructor_name",
    "operator_name",
    "qualified_identifier",
    "template_function",
}
_SPACE = re.compile(r"\s+")


def _is_decision(node) -> bool:
    if node.type in _DECISIONS:
        return True
    if node.type == "case_statement":
        return node.child_by_field_name("value") is not None
    if node.type == "binary_expression":
        operator = node.child_by_field_name("operator")
        return operator is not None and operator.type in _LOGIC
    return False


def _skip_nested(root):
    def skip(node) -> bool:
        return node.type in _UNITS and node.id != root.id

    return skip


def _has_body(node) -> bool:
    body = node.child_by_field_name("body")
    return body is not None and body.type in _BODIES


def _function_declarator(node):
    """Innermost function_declarator naming `node`, through */&/() wrappers."""

    current = node.child_by_field_name("declarator")
    while current is not None:
        if current.type == "function_declarator":
            inner = current.child_by_field_name("declarator")
            if inner is None or inner.type in _NAMES:
                return current
            current = inner
        elif current.type in _WRAPPERS:
            current = current.child_by_field_name("declarator") or next(
                iter(current.named_children), None
            )
        else:
            return None
    return None


def _scope_name(data: bytes, node) -> str:
    if node.type == "template_type":
        node = node.child_by_field_name("name") or node
    return node_text(data, node)


def _split_qualified(data: bytes, node) -> tuple[list[str], str]:
    scopes: list[str] = []
    while node.type == "qualified_identifier":
        scope = node.child_by_field_name("scope")
        if scope is not None:
            scopes.append(_scope_name(data, scope))
        node = node.child_by_field_name("name")
    return scopes, node_text(data, node)


def _name_of(data: bytes, node) -> tuple[list[str], str, object]:
    """(out-of-line scopes, name, parameter_list) of a function_definition."""

    declarator = _function_declarator(node)
    if declarator is None:
        # operator_cast and declarators a macro hides: keep the text, not nothing.
        raw = node.child_by_field_name("declarator")
        if raw is None:
            return [], f"<unnamed>@{start_line(node)}", None
        text = node_text(data, raw)
        params = next((n for n in descendants(raw) if n.type == "parameter_list"), None)
        return [], _SPACE.sub(" ", text.split("(")[0]).strip() or text, params
    name_node = declarator.child_by_field_name("declarator")
    scopes, name = _split_qualified(data, name_node)
    return scopes, name, declarator.child_by_field_name("parameters")


def _enclosing_scopes(data: bytes, node) -> list[str]:
    names: list[str] = []
    current = node.parent
    while current is not None:
        if current.type == "namespace_definition":
            named = current.child_by_field_name("name")
            if named is not None:
                names.extend(reversed(node_text(data, named).split("::")))
        elif current.type in _SCOPES:
            named = current.child_by_field_name("name")
            if named is not None:
                names.append(_scope_name(data, named))
        current = current.parent
    names.reverse()
    return [part.strip() for part in names if part.strip()]


def _parameter_types(data: bytes, params) -> str:
    if params is None:
        return "()"
    types = []
    for param in params.named_children:
        if param.type == "comment":
            continue
        cut = param.end_byte
        default = param.child_by_field_name("default_value")
        if default is not None:
            cut = default.start_byte
        text = data[param.start_byte : cut]
        ident = _parameter_identifier(param)
        if ident is not None and ident.end_byte <= cut:
            text = (
                data[param.start_byte : ident.start_byte] + data[ident.end_byte : cut]
            )
        cleaned = _SPACE.sub(" ", text.decode("utf-8")).strip().rstrip("=").strip()
        types.append(cleaned)
    return "(" + ", ".join(types) + ")"


def _parameter_identifier(param):
    current = param.child_by_field_name("declarator")
    while current is not None and current.type != "identifier":
        current = current.child_by_field_name("declarator") or next(
            (c for c in current.named_children if c.type == "identifier"), None
        )
    return current


def _file_namespace(path: str) -> str:
    return str(PurePosixPath(path.replace("\\", "/")).with_suffix(""))


def _owner(node):
    current = node.parent
    while current is not None:
        if current.type in _UNITS:
            return current
        current = current.parent
    return None


class _Reader:
    def __init__(self, data: bytes, path: str, language: str):
        self.data = data
        self.path = path
        self.language = language
        self.file_namespace = _file_namespace(path)
        self.named: dict[int, tuple[str, str]] = {}

    def namespace_for(self, node, scopes: list[str]) -> str:
        if self.language == "c":
            return self.file_namespace
        parts = [*_enclosing_scopes(self.data, node), *scopes]
        return "::".join(parts) if parts else self.file_namespace

    def identify(self, node):
        if node.type == "lambda_expression":
            owner = _owner(node)
            if owner is not None and owner.id in self.named:
                namespace, parent = self.named[owner.id]
                name = f"{parent}::lambda@{start_line(node)}"
            else:
                namespace = self.namespace_for(node, [])
                name = f"lambda@{start_line(node)}"
            return namespace, name, None
        scopes, name, params = _name_of(self.data, node)
        return self.namespace_for(node, scopes), name, params

    def read(self, root) -> list[Function]:
        found: list[tuple[Function, object]] = []
        for node in descendants(root):
            if node.type not in _UNITS:
                continue
            if node.type == "function_definition" and not _has_body(node):
                continue
            namespace, name, params = self.identify(node)
            self.named[node.id] = (namespace, name)
            found.append(
                (
                    Function(
                        name=name,
                        namespace=namespace,
                        complexity=complexity(node, _is_decision, _skip_nested(node)),
                        start_line=start_line(node),
                        end_line=end_line(node),
                        path=self.path,
                        language=self.language,
                        start_byte=node.start_byte,
                        end_byte=node.end_byte,
                    ),
                    params,
                )
            )
        return self.disambiguate(found)

    def disambiguate(self, found) -> list[Function]:
        counts: dict[tuple[str, str], int] = {}
        for function, _params in found:
            key = (function.namespace, function.name)
            counts[key] = counts.get(key, 0) + 1
        result = []
        for function, params in found:
            if counts[(function.namespace, function.name)] > 1 and params is not None:
                suffix = _parameter_types(self.data, params)
                function = replace(function, name=function.name + suffix)
            result.append(function)
        return result


def _functions(language: str, source: str, path: str) -> list[Function]:
    data, tree = parse(source, language)
    return _Reader(data, path, language).read(tree.root_node)


def c_functions(source: str, path: str, project_root: str | None = None) -> list[Function]:
    return _functions("c", source, path)


def cpp_functions(source: str, path: str, project_root: str | None = None) -> list[Function]:
    return _functions("cpp", source, path)
