"""Request lifecycle, transaction isolation, and metadata regression coverage."""

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import pytest

from astrolabe import Astrolabe, CaptureLimits, SpanCollector, aspan, get_collector, get_trace_context, span
from astrolabe.backends.sqlite import SQLiteBackend


def test_cancelled_request_is_recorded_and_context_restored():
    with Astrolabe(':memory:') as app:
        async def scenario():
            async def worker():
                async with app.atrace('ask') as collector:
                    async with aspan('hop'):
                        asyncio.current_task().cancel()
                        await asyncio.sleep(0)
                return collector
            task = asyncio.create_task(worker())
            with pytest.raises(asyncio.CancelledError):
                await task
            assert get_collector() is None
        asyncio.run(scenario())
        trace = app.list_traces()[0]
        assert trace['status'] == 'failed'
        nodes = app.get_trace_spans(trace['trace_id'])
        assert len(nodes) == 2
        assert all(s['status'] == 'error' for s in nodes)
        assert all(s['metadata']['error_type'] == 'CancelledError' for s in nodes)


@pytest.mark.parametrize('exception', [KeyboardInterrupt, SystemExit])
def test_base_exceptions_propagate_and_finish(exception):
    with Astrolabe(':memory:') as app:
        with pytest.raises(exception):
            with app.trace('job'):
                raise exception()
        assert app.list_traces()[0]['status'] == 'failed'
        assert get_collector() is None


def test_sql_failure_rolls_back_whole_batch_and_retry_is_safe():
    with Astrolabe(':memory:') as app:
        app.backend.conn.executescript("""CREATE TRIGGER break_write BEFORE INSERT ON trace_spans
            WHEN NEW.operation = 'bad' BEGIN SELECT RAISE(ABORT, 'injected failure'); END;""")
        with app.trace('ask') as collector:
            with span('good'):
                pass
            with span('bad'):
                pass
        assert collector.lifecycle == 'closed'
        assert not app.backend.conn.in_transaction
        assert app.count_traces() == 0
        assert app.get_trace_spans(collector.global_trace_id) == []
        with app.trace('next_request'):
            pass
        assert app.get_trace_spans(collector.global_trace_id) == []
        app.backend.conn.execute('DROP TRIGGER break_write')
        app.backend.conn.commit()
        assert collector.flush() == 3
        assert collector.flush() == 0
        assert len(app.get_trace_spans(collector.global_trace_id)) == 3


def test_metadata_serialization_validates_before_writing():
    backend = SQLiteBackend(':memory:')
    collector = SpanCollector(backend)
    try:
        with collector.span('ask'):
            pass
        # Direct backend calls still reject invalid JSON without executing SQL.
        record = dict(span_id='bad', trace_id='t', parent_span_id=None, operation='bad',
                      duration_ms=1, status='ok', metadata={'bad': object()}, created_at='')
        with pytest.raises(TypeError):
            backend.write_batch([], [record])
        assert not backend.conn.in_transaction
        assert backend.get_trace_spans('t') == []
        assert collector.flush() == 1
    finally:
        backend.close()


def test_batch_retry_after_commit_response_failure_is_idempotent():
    class LostResponse(SQLiteBackend):
        fail_once = True
        def _commit(self):
            super()._commit()
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError('commit succeeded but acknowledgement lost')
    backend = LostResponse(':memory:')
    collector = SpanCollector(backend)
    try:
        with collector.span('ask'):
            with collector.span('child'):
                pass
        with pytest.raises(RuntimeError):
            collector.flush()
        assert collector.flush() == 2
        assert len(backend.get_trace_spans(collector.global_trace_id)) == 2
    finally:
        backend.close()


def test_thread_batches_and_reads_do_not_interleave_transactions(tmp_path):
    with Astrolabe(str(tmp_path / 'threads.db')) as app:
        def worker(index):
            with app.trace('request', host=f'host-{index}') as collector:
                for n in range(5):
                    with span('step', index=n):
                        pass
            tree = app.build_span_tree(collector.global_trace_id)
            assert len(tree['tree']['children']) == 5
            return collector.global_trace_id
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(worker, range(64)))
        assert app.count_traces() == 64
        assert all(len(app.get_trace_spans(tid)) == 6 for tid in ids)
        assert not app.backend.conn.in_transaction


def test_collector_stacks_are_independent_even_with_direct_handles():
    with Astrolabe(':memory:') as app:
        outer, inner = app.create_collector('outer'), app.create_collector('inner')
        with outer.span('outer'):
            with inner.span('inner'):
                with outer.span('outer_child'):
                    pass
        outer.close()
        inner.close()
        assert app.build_span_tree(outer.global_trace_id)['tree']['children'][0]['operation'] == 'outer_child'
        assert app.build_span_tree(inner.global_trace_id)['tree']['children'] == []


def test_background_tasks_are_sealed_when_request_exits():
    with Astrolabe(':memory:') as app:
        async def scenario():
            started, resume = asyncio.Event(), asyncio.Event()
            async def worker():
                async with aspan('in_flight') as handle:
                    started.set()
                    await resume.wait()
                    handle.set_attrs(late='ignored')
                assert get_collector() is None
                assert get_trace_context() is None
                async with aspan('late') as late:
                    assert late.span_id == ''
            async with app.atrace('ask') as collector:
                task = asyncio.create_task(worker())
                await started.wait()
            resume.set()
            await task
            return collector
        collector = asyncio.run(scenario())
        nodes = app.get_trace_spans(collector.global_trace_id)
        assert [s['operation'] for s in nodes] == ['ask', 'in_flight']
        child = nodes[1]
        assert child['status'] == 'error'
        assert child['metadata']['error_type'] == 'IncompleteSpan'
        assert 'late' not in child['metadata']
        assert collector.flush() == 0
        assert app.get_trace(collector.global_trace_id)['metadata']['_astrolabe']['unfinished_spans'] == 1


def test_metadata_is_a_snapshot_and_finished_handles_are_frozen():
    with Astrolabe(':memory:') as app:
        arguments = {'q': ['original']}
        with app.trace('ask') as collector:
            with span('tool', args=arguments) as handle:
                arguments['q'].append('mutated')
                handle.set_attrs(result={'ids': ['a']})
            handle.set_attrs(after='ignored')
            collector.set_root_attrs(answer='done')
        collector.set_root_attrs(answer='overwritten')
        nodes = app.get_trace_spans(collector.global_trace_id)
        assert nodes[1]['metadata'] == {'args': {'q': ['original']}, 'result': {'ids': ['a']}}
        assert nodes[0]['metadata']['answer'] == 'done'


def test_invalid_metadata_is_diagnosed_without_losing_request():
    class Unfriendly:
        def __repr__(self):
            raise AssertionError('must not call repr')
        def __str__(self):
            raise AssertionError('must not call str')
    cyclic = []
    cyclic.append(cyclic)
    with Astrolabe(':memory:') as app:
        with app.trace('ask', weird=Unfriendly(), cycle=cyclic, infinity=float('inf')):
            pass
        trace = app.list_traces()[0]
        assert trace['status'] == 'done'
        md = trace['metadata']
        assert md['weird'] == '<unsupported Unfriendly>'
        assert md['cycle'] == ['<cycle or depth limit>']
        assert any('$.weird' in issue for issue in md['_astrolabe']['metadata_issues'])
        json.dumps(md, allow_nan=False)


def test_capture_limits_bound_spans_and_metadata():
    limits = CaptureLimits(max_spans=3, max_string_length=20, max_metadata_bytes=160)
    with Astrolabe(':memory:', limits=limits) as app:
        with app.trace('ask', huge='界' * 100, array=list(range(100))) as collector:
            for i in range(10):
                with span('step', index=i):
                    pass
        nodes = app.get_trace_spans(collector.global_trace_id)
        assert len(nodes) == 3
        assert nodes[0]['metadata']['_astrolabe']['dropped_spans'] == 8
        assert all(len(json.dumps(s['metadata']).encode()) <= 160 for s in nodes)


def test_multihost_queries_are_explicit_and_empty_host_is_selectable():
    with Astrolabe(':memory:') as app:
        for host in ('beta', 'alpha', ''):
            with app.trace(host + '_root', host=host, trace_id='shared'):
                with span(host + '_child'):
                    pass
        with pytest.raises(ValueError, match='multiple hosts'):
            app.get_trace('shared')
        with pytest.raises(ValueError, match='multiple hosts'):
            app.build_span_tree('shared')
        trees = app.build_span_trees('shared')
        assert len(trees) == 3
        for item in trees:
            assert item['tree']['operation'] == item['trace']['host'] + '_root'
            assert item['tree']['span_id'] == item['trace']['root_span_id']
        assert app.count_traces(host='') == 1
        assert len(app.get_trace_spans('shared', host='')) == 2


def test_filtered_subtree_preserves_start_order_not_breadth_first():
    with Astrolabe(':memory:') as app:
        with app.trace('root', host='app') as collector:
            with span('first'):
                with span('grandchild'):
                    pass
            with span('second'):
                pass
        assert [s['operation'] for s in app.get_trace_spans(collector.global_trace_id, host='app')] == [
            'root', 'first', 'grandchild', 'second']


def test_generic_operation_stats_and_count_share_filters():
    with Astrolabe(':memory:') as app:
        for host in ('a', 'b'):
            with app.trace('job', host=host, group=host):
                with span('custom.step'):
                    pass
        filters = {'group__eq': 'a'}
        assert app.count_traces(filters=filters) == len(app.list_traces(filters=filters)) == 1
        assert app.operation_stats(filters=filters, operation='custom.step')[0]['calls'] == 1
        assert app.stats(host='a')['overall']['total'] == 1
        assert app.stats()['tool_stats'] == []
        assert app.stats()['by_hops'] == []


def test_contains_is_literal_and_numeric_filters_accept_exponents():
    with Astrolabe(':memory:') as app:
        for text, value in [('100%_match', 300), ('100xxmatch', 10)]:
            with app.trace('ask', text=text, score=value):
                pass
        assert app.count_traces(filters={'text__contains': '%_'}) == 1
        assert app.count_traces(filters={'score__gte': '2e2'}) == 1


def test_deep_span_tree_builds_without_recursion():
    with Astrolabe(':memory:') as app:
        with app.trace('root') as collector:
            with ExitStack() as stack:
                for _ in range(1100):
                    stack.enter_context(span('nested'))
        node = app.build_span_tree(collector.global_trace_id)['tree']
        depth = 0
        while node['children']:
            node = node['children'][0]
            depth += 1
        assert depth == 1100


def test_readonly_backend_does_not_create_db_and_rejects_writes(tmp_path):
    missing = tmp_path / 'missing.db'
    with pytest.raises(sqlite3.OperationalError):
        SQLiteBackend(str(missing), readonly=True)
    assert not missing.exists()
    path = tmp_path / 'recorded.db'
    with Astrolabe(str(path)) as writer:
        with writer.trace('ask'):
            pass
    before = path.read_bytes()
    with Astrolabe(str(path), readonly=True) as reader:
        assert reader.count_traces() == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.backend.conn.execute('DELETE FROM traces')
    assert path.read_bytes() == before


def test_huge_integer_metadata_does_not_drop_trace():
    with Astrolabe(':memory:') as app:
        with app.trace('ask', huge=10**5000):
            pass
        assert app.count_traces() == 1
        # Python 3.10 may permit unlimited conversion; both are valid snapshots.
        trace = app.list_traces()[0]
        assert trace['status'] == 'done'
        assert isinstance(trace['metadata']['huge'], (int, str))


def test_truncation_preserves_capture_diagnostics_and_error_column():
    with Astrolabe(':memory:', limits=CaptureLimits(max_spans=1, max_metadata_bytes=128)) as app:
        with pytest.raises(RuntimeError):
            with app.trace('ask', fields={f'f{i}': 'xx' for i in range(20)}):
                with span('dropped'):
                    pass
                raise RuntimeError('failure' * 100)
        trace = app.list_traces()[0]
        assert trace['error'] == 'failure' * 100
        assert trace['metadata']['_astrolabe']['dropped_spans'] == 1
        assert len(json.dumps(trace['metadata']).encode()) <= 128


def test_different_root_cannot_replace_existing_request(caplog):
    with Astrolabe(':memory:') as app:
        with app.trace('ask', host='app', trace_id='shared', attempt=1) as first:
            with span('original'):
                pass
        original = app.get_trace('shared')
        with app.trace('ask', host='app', trace_id='shared', attempt=2) as second:
            with span('replacement'):
                pass
        assert first.root_span_id != second.root_span_id
        assert app.get_trace('shared') == original
        assert [s['operation'] for s in app.get_trace_spans('shared')] == ['ask', 'original']
        assert app.get_trace_spans('shared') == app.get_trace_spans('shared', host='app')
        assert app.operation_stats(operation='ask')[0]['calls'] == 1
        assert app.count_traces() == 1
        assert not app.backend.conn.in_transaction
        assert 'different root' in caplog.text
        with app.trace('next_request'):
            pass
        assert app.count_traces() == 2
        assert len(app.get_trace_spans('shared')) == 2


def _trace_record(trace_id, root, host='app'):
    return dict(trace_id=trace_id, host=host, root_span_id=root, status='done',
                duration_ms=1, error='', metadata={}, created_at='', updated_at='')


@pytest.mark.parametrize('preexisting', [False, True])
def test_conflicting_roots_reject_entire_batch(preexisting):
    with Astrolabe(':memory:') as app:
        first = _trace_record('shared', 'first')
        second = _trace_record('shared', 'second')
        if preexisting:
            app.backend.write_batch([first], [])
            batch = [_trace_record('unrelated', 'other'), second]
        else:
            batch = [_trace_record('unrelated', 'other'), first, second]
        with pytest.raises(ValueError, match='different root'):
            app.backend.write_batch(batch, [])
        assert app.get_trace('unrelated') is None
        assert app.count_traces() == int(preexisting)
        assert not app.backend.conn.in_transaction


def test_competing_connections_preserve_one_complete_request(tmp_path):
    path = str(tmp_path / 'competing.db')
    apps = [Astrolabe(path), Astrolabe(path)]
    try:
        def write(index):
            with apps[index].trace('ask', host='app', trace_id='shared', writer=index):
                with span(f'child-{index}'):
                    pass
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(write, (0, 1)))
        for app in apps:
            trace = app.get_trace('shared')
            nodes = app.get_trace_spans('shared')
            assert app.count_traces() == 1
            assert [s['operation'] for s in nodes] == ['ask', f"child-{trace['metadata']['writer']}"]
            assert nodes[0]['span_id'] == trace['root_span_id']
            assert app.operation_stats(operation='ask')[0]['calls'] == 1
    finally:
        for app in apps:
            app.close()


def test_string_limits_preserve_original_diagnostic_paths():
    with Astrolabe(':memory:', limits=CaptureLimits(max_string_length=8)) as app:
        with app.trace('ask', text='a' * 40, unsupported=object()) as request:
            request.set_root_attrs(other='b' * 40)
        metadata = app.get_trace(request.global_trace_id)['metadata']
        assert metadata['text'] == 'a' * 8
        assert metadata['other'] == 'b' * 8
        issues = metadata['_astrolabe']['metadata_issues']
        assert '$.text: string truncated' in issues
        assert '$.other: string truncated' in issues
        assert '$.unsupported: unsupported object' in issues
        assert not any('$._astrolabe.metadata_issues' in issue for issue in issues)
        assert app.get_trace_spans(request.global_trace_id)[0]['metadata'] == metadata


def test_metadata_updates_accumulate_issues_without_recapturing_old_values():
    with Astrolabe(':memory:') as app:
        with app.trace('ask', initial=object()) as request:
            request.set_root_attrs(second=object())
            request.set_root_attrs(result={'ids': ['one']})
        issues = app.get_trace(request.global_trace_id)['metadata']['_astrolabe']['metadata_issues']
        assert issues == ['$.initial: unsupported object', '$.second: unsupported object']


def test_final_limits_apply_after_attribute_updates_not_before():
    with Astrolabe(':memory:', limits=CaptureLimits(max_metadata_bytes=128)) as app:
        with app.trace('ask', field='a' * 1000) as request:
            request.set_root_attrs(field='short')
        metadata = app.get_trace(request.global_trace_id)['metadata']
        assert metadata == {'field': 'short'}


def test_storage_retry_preserves_frozen_metadata_diagnostics():
    class FailOnce(SQLiteBackend):
        fail_once = True
        def _commit(self):
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError('temporary failure')
            super()._commit()
    backend = FailOnce(':memory:')
    try:
        with Astrolabe(backend=backend, limits=CaptureLimits(max_string_length=8)) as app:
            with app.trace('ask', text='x' * 40) as request:
                pass
            assert app.count_traces() == 0
            assert request.flush() == 1
            assert request.flush() == 0
            metadata = app.get_trace(request.global_trace_id)['metadata']
            assert metadata['_astrolabe']['metadata_issues'] == ['$.text: string truncated']
            assert metadata['text'] == 'x' * 8
    finally:
        backend.close()


def test_host_cannot_overwrite_reserved_diagnostics():
    with Astrolabe(':memory:') as app:
        with app.trace('ask', _astrolabe={'dropped_spans': 999}, bad=object()) as request:
            pass
        diag = app.get_trace(request.global_trace_id)['metadata']['_astrolabe']
        assert 'dropped_spans' not in diag
        assert '$._astrolabe: reserved key omitted' in diag['metadata_issues']
        assert '$.bad: unsupported object' in diag['metadata_issues']
