"""The old repository script delegates query and rendering to one CLI."""

import io

import pytest

from astrolabe import Astrolabe, span
from astrolabe.cli import main as cli_main
from tui import main


@pytest.fixture
def demo_db(tmp_path):
    path = tmp_path / 'traces.db'
    with Astrolabe(str(path)) as app:
        for host in ('alpha', 'beta'):
            with app.trace('ask', host=host, trace_id='shared', question='什么是 RAG？'):
                with span('hop', hop_number=1):
                    with span('tool.search', tool_args={'q': 'RAG'}):
                        pass
    return path


@pytest.mark.parametrize('legacy, canonical', [
    (['--list'], ['list']),
    (['--list', '--host', 'alpha'], ['list', '--host', 'alpha']),
    (['--trace', 'shared'], ['show', 'shared']),
    (['--trace', 'shared', '--host', 'beta'], ['show', 'shared', '--host', 'beta']),
])
def test_legacy_arguments_use_identical_output(demo_db, legacy, canonical):
    outputs = []
    for entry, args in ((main, legacy), (cli_main, canonical)):
        out, err = io.StringIO(), io.StringIO()
        code = entry(['--db', str(demo_db), *args], out=out, err=err)
        outputs.append((code, out.getvalue(), err.getvalue()))
    assert outputs[0] == outputs[1]
    assert outputs[0][0] == 0
    assert 'shared' in outputs[0][1]


def test_main_missing_trace_does_not_open_database(tmp_path):
    path = tmp_path / 'missing.db'
    err = io.StringIO()
    assert main(['--db', str(path)], err=err) == 2
    assert 'required' in err.getvalue()
    assert not path.exists()


def test_main_unknown_trace(demo_db):
    err = io.StringIO()
    assert main(['--db', str(demo_db), '--trace', 'missing'], err=err) == 1
    assert 'trace not found' in err.getvalue()


def test_main_missing_database(tmp_path):
    path = tmp_path / 'missing.db'
    err = io.StringIO()
    assert main(['--db', str(path), '--list'], err=err) == 1
    assert err.getvalue().startswith('error:')
    assert not path.exists()
