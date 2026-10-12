"""Installed CLI behavior exercised against real temporary database files."""

import io
import json
import subprocess
import sys

import pytest

from astrolabe import Astrolabe, span
from astrolabe.cli import main


@pytest.fixture
def recording(tmp_path):
    path = tmp_path / 'recording.db'
    with Astrolabe(str(path)) as app:
        for host in ('alpha', 'beta'):
            with app.trace('ask', host=host, trace_id='shared', question='树状检索过程'):
                with span('hop', hop_number=1, llm_prompt_tokens=12, llm_completion_tokens=3):
                    with span('tool.search', tool_args={'q': 'RAG'}, result_count=2) as tool:
                        pass
                tool_id = tool.span_id
    return path, tool_id


def run_cli(path, *args):
    out, err = io.StringIO(), io.StringIO()
    status = main(['--db', str(path), *args], out=out, err=err)
    return status, out.getvalue(), err.getvalue()


def test_list_filters_and_total(recording):
    path, _ = recording
    rc, out, err = run_cli(path, 'list', '--host', 'alpha', '--filter', 'question__contains=树状', '--json')
    assert rc == 0 and not err
    payload = json.loads(out)
    assert payload['total'] == 1
    assert payload['traces'][0]['host'] == 'alpha'


def test_show_all_hosts_and_nested_tools(recording):
    path, _ = recording
    rc, out, err = run_cli(path, 'show', 'shared')
    assert rc == 0 and not err
    assert 'host=alpha' in out and 'host=beta' in out
    assert out.count('└── tool.search') == 2
    assert 'recorded tokens: input=12 output=3' in out
    assert 'Durations include children' in out


def test_show_host_and_full_details(recording):
    path, _ = recording
    rc, out, err = run_cli(path, 'show', 'shared', '--host', 'beta', '--details')
    assert rc == 0 and not err
    assert 'host=alpha' not in out
    assert '"tool_args"' in out and '"q": "RAG"' in out


def test_show_latest_requests_and_json_export(recording):
    path, _ = recording
    rc, out, err = run_cli(path, 'show', '--latest', '2', '--json')
    assert rc == 0 and not err
    payload = json.loads(out)
    assert len(payload) == 2
    assert {item['trace']['host'] for item in payload} == {'alpha', 'beta'}
    assert all(len(item['spans']) == 3 for item in payload)
    rc, out, err = run_cli(path, 'show', '--host', 'alpha')
    assert rc == 0 and out.count('trace=shared') == 1


def test_span_inspection(recording):
    path, sid = recording
    rc, out, err = run_cli(path, 'span', 'shared', sid, '--host', 'beta', '--json')
    assert rc == 0 and not err
    payload = json.loads(out)
    assert payload['metadata']['tool_args'] == {'q': 'RAG'}
    assert payload['operation'] == 'tool.search'


def test_stats_exact_operation_filter(recording):
    path, _ = recording
    rc, out, err = run_cli(path, 'stats', '--host', 'alpha', '--operation', 'tool.search', '--json')
    assert rc == 0 and not err
    payload = json.loads(out)
    assert payload['overall']['total'] == 1
    assert len(payload['operations']) == 1
    assert payload['operations'][0]['calls'] == 1


def test_database_options_work_before_or_after_subcommand(recording):
    path, _ = recording
    out = io.StringIO()
    assert main(['list', '--db', str(path), '--json'], out=out) == 0
    assert json.loads(out.getvalue())['total'] == 2


@pytest.mark.parametrize('args', [('show', 'missing'), ('show', 'shared', '--host', 'missing'),
                                  ('span', 'shared', 'missing'), ('list', '--filter', 'broken'),
                                  ('list', '--filter', 'q__unsupported=x'), ('show', '--latest', '0'),
                                  ('show', 'shared', '--latest', '2'), ('list', '--limit', '-1')])
def test_errors_are_actionable_without_tracebacks(recording, args):
    path, _ = recording
    rc, out, err = run_cli(path, *args)
    assert rc == 1
    assert out == '' and err.startswith('error: ')
    assert 'Traceback' not in err


def test_empty_database_and_missing_file(tmp_path):
    path = tmp_path / 'missing.db'
    rc, _, err = run_cli(path, 'list')
    assert rc == 1 and 'error:' in err
    assert not path.exists()
    with Astrolabe(str(path)):
        pass
    rc, out, err = run_cli(path, 'show')
    assert rc == 0 and out == 'No matching requests.\n' and not err


def test_terminal_controls_are_escaped(tmp_path):
    path = tmp_path / 'controls.db'
    with Astrolabe(str(path)) as app:
        with app.trace('ask\x1b[31m', question='line\nnext\x1b[2J', value='\x9b31m'):
            pass
    rc, out, err = run_cli(path, 'show', '--details')
    assert rc == 0 and not err
    assert '\x1b' not in out and '\x9b' not in out
    assert '\\u001b' in out


def test_module_and_installed_entry_point(recording):
    path, _ = recording
    for invocation in ([sys.executable, '-m', 'astrolabe'], ['astrolabe']):
        result = subprocess.run([*invocation, '--db', str(path), 'list', '--json'],
                                capture_output=True, text=True, check=True)
        assert json.loads(result.stdout)['total'] == 2
        assert result.stderr == ''
