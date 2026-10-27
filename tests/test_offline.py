"""QUALITY_TOOLS_OFFLINE: no grammar download, and a missing grammar fails by name."""

import sys
import types

import pytest

from crapper.languages.treesitter import GrammarUnavailable, parser_for


class FakePack(types.ModuleType):
    def __init__(self, on_disk):
        super().__init__("tree_sitter_language_pack")
        self.on_disk = list(on_disk)
        self.downloads = []
        self.parsers = []

    def downloaded_languages(self):
        return list(self.on_disk)

    def cache_dir(self):
        return "/prepared/cache"

    def download(self, names):
        self.downloads.append(list(names))
        return len(names)

    def get_parser(self, name):
        if name not in self.on_disk:
            self.downloads.append([name])
        self.parsers.append(name)
        return f"parser:{name}"


@pytest.fixture
def pack(monkeypatch):
    def install(on_disk):
        fake = FakePack(on_disk)
        monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", fake)
        return fake

    parser_for.cache_clear()
    yield install
    parser_for.cache_clear()


def test_offline_with_the_grammar_on_disk_loads_it_without_downloading(pack, monkeypatch):
    monkeypatch.setenv("QUALITY_TOOLS_OFFLINE", "1")
    fake = pack(["c"])
    assert parser_for("c") == "parser:c"
    assert fake.downloads == []


def test_offline_with_the_grammar_missing_names_it_and_downloads_nothing(pack, monkeypatch):
    monkeypatch.setenv("QUALITY_TOOLS_OFFLINE", "1")
    fake = pack(["c"])
    with pytest.raises(GrammarUnavailable, match="'cpp'.*QUALITY_TOOLS_OFFLINE"):
        parser_for("cpp")
    assert fake.downloads == []
    assert fake.parsers == []


def test_default_run_still_downloads_as_upstream_does(pack, monkeypatch):
    monkeypatch.delenv("QUALITY_TOOLS_OFFLINE", raising=False)
    fake = pack([])
    assert parser_for("c") == "parser:c"
    assert fake.downloads[0] == ["c"]


def test_zero_means_online(pack, monkeypatch):
    monkeypatch.setenv("QUALITY_TOOLS_OFFLINE", "0")
    fake = pack([])
    parser_for("c")
    assert fake.downloads[0] == ["c"]


def test_the_command_line_reports_a_missing_grammar_and_exits_1(monkeypatch, capsys):
    import crapper.cli as cli

    def missing(_argv):
        raise GrammarUnavailable("tree-sitter grammar 'c' is not in /x")

    monkeypatch.setattr(cli, "run", missing)
    with pytest.raises(SystemExit) as exit_:
        cli.main([])
    assert exit_.value.code == 1
    assert "grammar 'c'" in capsys.readouterr().err
