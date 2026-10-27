# crapper

CRAP scores for Clojure, Java, Go, TypeScript, Rust, Python, C, and C++. One run detects the language of each source file, applies that language's complexity and coverage rules, and writes the snapshot [uml-viewer](https://github.com/unclebob/uml-viewer) already reads.

The formula is the one from [crap4clj](https://github.com/unclebob/crap4clj), [crap4java](https://github.com/unclebob/crap4java), and [crap4go](https://github.com/unclebob/crap4go):

```text
CRAP = CC² × (1 − coverage)³ + CC
```

`CC` is cyclomatic complexity. `coverage` is the fraction of the function exercised by tests. A score of 1–5 is low risk, 5–30 is worth a look, and 30+ is complex and under-tested.

## Run

```bash
./crapper
```

The first run creates `.venv` and installs the tool. From a project root it walks the tree, skips `test`, `spec`, `vendor`, `node_modules`, and `target`, and writes two results:

- a table on stdout, worst score first
- `.metrics/crap.edn`, replaced on every successful analysis

```bash
./crapper --no-coverage                  # complexity only; coverage is N/A
./crapper --use-existing-coverage        # read reports already on disk
./crapper src/demo/core.clj src/ui       # these files and trees
./crapper --changed                      # git additions and edits
./crapper combat                         # path fragment, same idea as crap4clj
./crapper --threshold 30                 # exit 2 when the worst score is higher
```

## Snapshot

`.metrics/crap.edn` has the same shape crap4clj writes:

```clojure
{:entries [{:name "place"
            :namespace "demo.Board"
            :complexity 3
            :coverage 75.0
            :crap 3.1}]}
```

uml-viewer groups entries by `:namespace` and joins each operation on `:name`. `nil` coverage is what `--no-coverage` writes: complexity only, and CRAP is `nil` too. A function the coverage report does not mention scores 0%.

| Language | `:namespace` | `:name` |
| --- | --- | --- |
| Clojure | the `ns` | the `defn` / `defn-` name |
| Java | `package.Class`, or `package.Outer.Inner` | the method name |
| Go | the package import path, or `import/path.Receiver` | the function or method name |
| TypeScript | the dotted module path, or `module.Class` | the function or method name |
| Rust | `crate::module`, or `crate::module::Type` | the function or method name |
| Python | the dotted module path, or `module.Class` | the function or method name |
| C | the file's project-relative path without its extension (`src/render/gl`) | the function name |
| C++ | `ns::Class`, or the file path for a global function | the function or method name, `add(int, int)` for an overload |

Rename or move is a new entry. There is no identity matching across runs.

## What each language counts

Clojure follows crap4clj: `if` / `when` and their variants, `and`, `or`, `loop`, `catch`, and each clause of `cond`, `condp`, `case`, `cond->`, `cond->>`, `some->`, and `some->>`. Coverage prefers Cloverage's per-line form counts and falls back to LCOV.

Java follows crap4java: `if`, loops, `catch`, `?:`, each `switch` label, and `&&` / `||`. Constructors and methods inside anonymous classes are omitted. Coverage is JaCoCo's `INSTRUCTION` counter.

Go follows crap4go: `if`, `for`, `range`, each `switch` and `select` clause, and `&&` / `||`. Coverage is `go test -coverprofile`.

TypeScript, TSX, and JavaScript (`.js`, `.jsx`, `.mjs`, `.cjs`) use the same structural decisions as Java, including `&&` inside JSX, plus `??` and `?.`. Top-level functions, class methods, and top-level arrow functions are entries. An inline Express callback — `.get`, `.post`, `.put`, `.patch`, `.delete`, `.head`, `.options`, `.all`, `.use`, and `.route(path).get(...)` — is its own entry, named `GET /users`. A second callback on that same route is `GET /users#2`. Its decisions are not also charged to the enclosing function. Other nested callbacks stay inside the enclosing function. Coverage is LCOV. When the report has branch records (`BRDA`) inside a function, the score uses those branches; a function with no branches uses line hits. A `coverage` script is used as-is. A Vitest project runs `vitest --coverage` (installing `@vitest/coverage-v8` into `node_modules` when it is missing, without editing `package.json`). Other test scripts are wrapped in `c8`.

Rust counts `if`, loops, each `match` arm, `?`, and `&&` / `||`. `mod tests` is skipped. Coverage is LCOV from `cargo llvm-cov` or `cargo tarpaulin`, run in the nearest directory that contains `Cargo.toml`. When neither tool is installed, the run installs `cargo-llvm-cov`.

Python counts `if`, `elif`, `for`, `while`, `except`, each `match` case, a comprehension filter, a conditional expression, and each `and` / `or`. Nested functions stay inside the enclosing function. Coverage is LCOV from `coverage.py`, running pytest when the project uses it and `unittest` otherwise. `coverage` and `pytest` are installed into the project's interpreter when they are missing.

C and C++ (`.c` and `.h` are C; `.cc`, `.cpp`, `.cxx`, `.hpp`, `.hh`, and `.hxx` are C++) count `if`, `for` and range-`for`, `while`, `do`, each `case` label but not `default`, `?:`, `&&` / `||` (and `and` / `or`), and `catch`. Only a definition with a body is an entry: prototypes, member declarations, and `= default` / `= delete` are not. Preprocessor conditionals are not expanded and are not decisions; both sides of an `#if` are scored as written, and code a macro expands to is not seen. A lambda is its own entry, `function::lambda@<line>`, and its decisions are not charged to the enclosing function. C++ namespaces come from the enclosing namespaces and classes, and from the scope of an out-of-line definition (`int Widget::get()` is in `Widget`); anonymous namespaces add nothing. A C function, and a C++ function in the global namespace, is namespaced by its file path, so a `.c` file and its `.h` share one. When one file defines a name twice in one namespace, each entry gets its parameter types appended (`add(int, int)`, `add(double)`); overloads spread over different files are not told apart. A C++ header named `.h` is parsed as C and scores badly; name it `.hpp`. Coverage comes from the CMake project that owns the file: the nearest `CMakeLists.txt` above it that calls `project(`. That project is configured and built in `target/crapper-build/c/`, never in its own build directory, with `-O0` and coverage flags; its tests run through `ctest`; and the counters are exported as LCOV and joined to functions by file and line range. CMake's own compiler detection picks the tools. Clang (including Apple clang, whose tools come from `xcrun`) uses source-based coverage, `-fprofile-instr-generate -fcoverage-mapping` read by `llvm-profdata` and `llvm-cov export -format=lcov`. GCC, or Clang without `llvm-profdata`, builds with `--coverage` and reads the `.gcda` counters with `gcovr`, else `lcov` (when gcov is a single command), else `gcov` (or `llvm-cov gcov`) itself. Without a CMake project, nothing is built and C and C++ score 0% with a warning. A missing tool, a failed configure or build, a failing or empty test run (`ctest --no-tests=error`), or tests that recorded no counters (an uninstrumented build) all write no report, so C and C++ score 0%; unlike Python, coverage from a failing test run is not kept. A project whose tests are not registered with `ctest` can pass `--coverage-command` to write its own LCOV under `target/coverage/`.

## Coverage commands

By default a run deletes the previous report for each language it is about to measure and regenerates it:

| Language | Command | Report |
| --- | --- | --- |
| Clojure | `clj -M:cov --lcov` (needs `deps.edn` or `bb.edn`) | `target/coverage/` |
| Java | JaCoCo Maven plugin `0.8.12` in each module with `pom.xml` | `target/site/jacoco/jacoco.xml` |
| Go | `go test ./... -coverprofile=...` | `target/coverage/go/coverage.out` |
| TypeScript | `npm run coverage`, or Vitest `--coverage`, or `npx c8 ... npm test` | `coverage/lcov.info` or `target/coverage/typescript/lcov.info` |
| Rust | `cargo llvm-cov` or `cargo tarpaulin`, per Cargo package | `target/coverage/rust/lcov.info` |
| Python | `coverage run` with pytest or unittest, then `coverage lcov` | `target/coverage/python/lcov.info` |
| C, C++ | `cmake` configure and build with coverage flags in `target/crapper-build/c/`, `ctest`, then `llvm-cov export` (Clang) or gcovr / lcov / `gcov` (GCC), per CMake project | `target/coverage/c/lcov.info` |

A missing tool or a failed test run scores that language at 0% and still writes the snapshot. Pass `--coverage-command` to replace those defaults with one command of your own. `--no-coverage` is the run that leaves coverage and CRAP as N/A.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
