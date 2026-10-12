"""Create three explicitly simulated requests; never calls a model or the network."""

import argparse
import asyncio

from astrolabe import Astrolabe, aspan


async def record(app: Astrolabe, question: str, *, fail: bool = False):
    async with app.atrace('ask', host='demo', question=question, simulated=True) as request:
        print(request.global_trace_id)
        async with aspan('hop', hop_number=1):
            async with aspan('llm.chat', model='simulated-model') as model:
                await asyncio.sleep(0.02)
                model.set_attrs(llm_prompt_tokens=120, llm_completion_tokens=32,
                                content='模拟响应：调用检索工具。')
            async with aspan('tool.search', tool_args={'q': question}) as tool:
                await asyncio.sleep(0.02)
                if fail:
                    raise RuntimeError('模拟检索超时，用于查看失败树')
                tool.set_attrs(result_count=2, result_summary='模拟新闻 A、模拟新闻 B')
        request.set_root_attrs(answer='模拟答案：检索已完成。', hop_count=1)


async def run(path: str):
    with Astrolabe(path) as app:
        await record(app, '最近有哪些新闻？')
        await record(app, '哪些新闻与 AI 有关？')
        try:
            await record(app, '一次失败的检索是什么样？', fail=True)
        except RuntimeError:
            pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, help='database to create/append simulated records to')
    asyncio.run(run(parser.parse_args().db))
