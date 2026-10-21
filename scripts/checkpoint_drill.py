"""Redis 检查点跨进程恢复演练（方案 §8）。

真实 Redis Stack 环境下的可执行演练（本仓库默认 compose 的 redis-stack-server）：

1. 前置检查：redis PING、RediSearch（FT._LIST）可用、bypass 配置的强制 redis 后端可装配。
2. 阶段 A（子进程 1）：装配真实检查点后端，运行带人工审批门（interrupt）的演练图，
   在断点暂停并把 thread_id、压缩摘要字段与消息序列落盘。
3. 阶段 B（新子进程，模拟重启后的 API 进程）：仅凭 thread_id 经 Command(resume=...) 恢复，
   核对：检查点存在、消息与摘要字段跨进程一致、审批决策已写入。
4. 保留与清理策略：抽样检查点键的 TTL，核对与 CHECKPOINT_TTL_MINUTES 一致；
   0 表示不过期（需运维手动清理）。

说明：演练图与 Supervisor 的审批门同构（messages + summary + interrupt + Command(resume)），
验证的是检查点后端本身的跨进程恢复能力；端到端 API 演练需要完整服务与数据库，
在环境就绪后另行执行（见 docs/validation.md）。

用法：
  docker compose up -d redis
  python scripts/checkpoint_drill.py --run                 # 全流程 + 报告
  python scripts/checkpoint_drill.py --check               # 只做环境检查
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORTS_DIR = ROOT / "evals" / "reports"
DEFAULT_DECISION = {"decision": "approve", "by": "checkpoint-drill"}


class DrillState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    summary: str
    answer: dict
    approval: dict


def _prepare(state: DrillState) -> dict:
    """确定性产出待人工审批的答案与压缩摘要字段（与 composer 的状态同构）。"""
    return {
        "summary": "早期对话摘要：买家咨询吸顶灯尺寸与退货窗口；" * 3,
        "answer": {"agent": "aftersale", "message": "建议换货，需人工审核后创建工单",
                   "pending_confirmation": True},
    }


def _review(state: DrillState) -> dict:
    from langgraph.types import interrupt

    decision = interrupt({"type": "human_approval", "answer": state.get("answer", {})})
    approval = decision if isinstance(decision, dict) else {"decision": str(decision)}
    return {"approval": approval,
            "answer": {**state.get("answer", {}), "approval": approval}}


def _route(state: DrillState) -> str:
    return "review" if state.get("answer", {}).get("pending_confirmation") else "__end__"


def build_drill_graph(checkpointer=None):
    """构建与 Supervisor 审批门同构的演练图（挂真实检查点后端）。"""
    from langgraph.graph import END, START, StateGraph

    from src.agents.checkpoint import build_checkpointer

    graph = StateGraph(DrillState)
    graph.add_node("prepare", _prepare)
    graph.add_node("review", _review)
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", _route, {"review": "review", END: END})
    graph.add_edge("review", END)
    return graph.compile(checkpointer=checkpointer or build_checkpointer(), name="checkpoint_drill")


async def _configure_redis_backend():
    """演练强制 redis 后端（Redis 不可用直接报错，不静默回退内存）。"""
    from src.agents.checkpoint import checkpoint_backend, configure_checkpointer
    from src.core.config import get_settings

    settings = get_settings().model_copy(update={"CHECKPOINT_BACKEND": "redis"})
    backend = await configure_checkpointer(settings)
    return settings, backend


def _serialize_messages(messages) -> list[dict]:
    return [{"type": getattr(message, "type", "unknown"), "content": message.content}
            for message in messages]


async def run_pause(thread_id: str | None = None) -> dict:
    """阶段 A：跑到审批门暂停，返回可跨进程恢复所需的快照。"""
    from langchain_core.messages import HumanMessage

    from src.agents.checkpoint import checkpoint_backend, new_thread_id, thread_config

    _, backend = await _configure_redis_backend()
    thread_id = thread_id or new_thread_id()
    graph = build_drill_graph()
    config = thread_config(thread_id)
    messages = [HumanMessage(content=f"第 {index} 轮：咨询尺寸、运费与退货窗口")
                for index in range(1, 7)]
    state = await graph.ainvoke({"messages": messages, "summary": ""}, config)
    interrupts = state.get("__interrupt__") or []
    snapshot = await graph.aget_state(config)
    return {
        "stage": "pause",
        "backend": backend,
        "thread_id": thread_id,
        "interrupted": bool(interrupts),
        "interrupt": interrupts[0].value if interrupts else None,
        "summary": snapshot.values.get("summary", ""),
        "messages": _serialize_messages(snapshot.values.get("messages", [])),
        "checkpoint_id": (snapshot.config.get("configurable") or {}).get("checkpoint_id"),
        "paused_at": datetime.now(timezone.utc).isoformat(),
    }


async def run_resume(thread_id: str, decision: dict) -> dict:
    """阶段 B（新进程）：凭 thread_id 恢复审批并核对上下文一致性。"""
    from langgraph.types import Command

    from src.agents.checkpoint import checkpoint_backend, thread_config

    _, backend = await _configure_redis_backend()
    graph = build_drill_graph()
    config = thread_config(thread_id)
    restored = await graph.aget_state(config)
    state = await graph.ainvoke(Command(resume=decision), config)
    return {
        "stage": "resume",
        "backend": backend,
        "thread_id": thread_id,
        "found_checkpoint": bool(restored.values),
        "restored_summary": restored.values.get("summary", ""),
        "restored_messages": _serialize_messages(restored.values.get("messages", [])),
        "summary": state.get("summary", ""),
        "messages": _serialize_messages(state.get("messages", [])),
        "approval": state.get("approval"),
        "resumed_at": datetime.now(timezone.utc).isoformat(),
    }


def validate_drill_pair(pause: dict, resume: dict) -> list[str]:
    """核对跨进程恢复与摘要一致性；返回问题列表（空表示通过）。"""
    problems: list[str] = []
    if pause.get("backend") != "redis" or resume.get("backend") != "redis":
        problems.append("演练未运行在 redis 检查点后端")
    if not pause.get("interrupted"):
        problems.append("阶段 A 未在人工审批门暂停（interrupt 未触发）")
    if pause.get("thread_id") != resume.get("thread_id"):
        problems.append("thread_id 不一致")
    if not resume.get("found_checkpoint"):
        problems.append("新进程未找到持久化检查点")
    if resume.get("restored_messages") != pause.get("messages"):
        problems.append("跨进程恢复的消息序列不一致")
    if resume.get("restored_summary") != pause.get("summary"):
        problems.append("跨进程恢复的摘要字段不一致（压缩上下文丢失）")
    if (resume.get("approval") or {}).get("decision") != "approve":
        problems.append("审批决策未写回检查点")
    return problems


async def check_environment() -> dict:
    """前置检查：Redis 可达 + RediSearch 可用 + 强制 redis 后端可装配。"""
    import redis.asyncio as aioredis

    from src.core.config import get_settings

    settings = get_settings()
    client = aioredis.from_url(settings.redis_url)
    report: dict = {"redis_url": settings.redact_redis_url,
                    "ttl_minutes": settings.CHECKPOINT_TTL_MINUTES}
    try:
        report["redis_ping"] = bool(await client.ping())
        try:
            indexes = await client.execute_command("FT._LIST")
            report["redi_search"] = True
            report["existing_indexes"] = [item.decode() if isinstance(item, bytes) else str(item)
                                          for item in indexes]
        except Exception as exc:
            report["redi_search"] = False
            report["redi_search_error"] = type(exc).__name__
    except Exception as exc:
        report["redis_ping"] = False
        report["redis_error"] = type(exc).__name__
    finally:
        await client.aclose()
    try:
        _, backend = await _configure_redis_backend()
        report["checkpoint_backend"] = backend
    except Exception as exc:
        report["checkpoint_backend"] = f"装配失败：{type(exc).__name__}"
    report["ok"] = bool(report.get("redis_ping")) and bool(report.get("redi_search")) \
        and report.get("checkpoint_backend") == "redis"
    return report


async def sample_checkpoint_ttl(samples: int = 5) -> dict:
    """抽样检查点键的 TTL，核对保留策略（-1 表示不过期）。"""
    import redis.asyncio as aioredis

    from src.core.config import get_settings

    settings = get_settings()
    client = aioredis.from_url(settings.redis_url)
    try:
        keys: list[bytes] = []
        async for key in client.scan_iter(match="checkpoint*", count=200):
            keys.append(key)
            if len(keys) >= samples:
                break
        ttl_seconds = []
        for key in keys:
            ttl_seconds.append(await client.ttl(key))
        expected = settings.CHECKPOINT_TTL_MINUTES * 60
        return {
            "sampled_keys": [key.decode(errors="replace") for key in keys],
            "ttl_seconds": ttl_seconds,
            "expected_ttl_seconds": expected if settings.CHECKPOINT_TTL_MINUTES > 0 else None,
            "policy": (f"TTL {settings.CHECKPOINT_TTL_MINUTES} 分钟，读取续期"
                       if settings.CHECKPOINT_TTL_MINUTES > 0 else "不过期（0），需运维手动清理"),
        }
    finally:
        await client.aclose()


def _run_stage(args: list[str]) -> int:
    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), *args],
                               cwd=ROOT, capture_output=True, text=True, timeout=300)
    if completed.stdout.strip():
        print(completed.stdout.strip())
    if completed.returncode != 0:
        print(f"[drill] 子进程失败（{' '.join(args)}）：{completed.stderr.strip()[:500]}")
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Redis 检查点跨进程恢复演练（方案 §8）")
    parser.add_argument("--run", action="store_true", help="全流程：环境检查 → 子进程暂停 → 新进程恢复 → 报告")
    parser.add_argument("--check", action="store_true", help="只做环境检查")
    parser.add_argument("--pause", action="store_true", help="阶段 A（子进程内部使用）")
    parser.add_argument("--resume", action="store_true", help="阶段 B（子进程内部使用）")
    parser.add_argument("--state-file", help="阶段 A 落盘 / 阶段 B 读取的 JSON 路径")
    parser.add_argument("--out", help="阶段 B 输出路径")
    parser.add_argument("--decision", default="approve", help="审批决策（approve/reject）")
    parser.add_argument("--report", help="报告输出路径（默认写入 evals/reports/）")
    args = parser.parse_args(argv)

    if args.check:
        report = asyncio.run(check_environment())
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    if args.pause:
        if not args.state_file:
            parser.error("--pause 需要 --state-file")
        payload = asyncio.run(run_pause())
        Path(args.state_file).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
        print(f"[drill] 阶段 A 完成：thread_id={payload['thread_id']} interrupted={payload['interrupted']}")
        return 0 if payload["interrupted"] else 1

    if args.resume:
        if not (args.state_file and args.out):
            parser.error("--resume 需要 --state-file 与 --out")
        pause = json.loads(Path(args.state_file).read_text(encoding="utf-8"))
        decision = {"decision": args.decision, "by": DEFAULT_DECISION["by"]}
        payload = asyncio.run(run_resume(pause["thread_id"], decision))
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[drill] 阶段 B 完成：found_checkpoint={payload['found_checkpoint']}")
        return 0 if payload["found_checkpoint"] else 1

    if not args.run:
        parser.print_help()
        return 2

    environment = asyncio.run(check_environment())
    print(json.dumps(environment, ensure_ascii=False, indent=2))
    if not environment["ok"]:
        print("[drill] 环境未就绪：请先启动 Redis Stack（docker compose up -d redis）")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work_dir = REPORTS_DIR / f"drill-{stamp}"
    work_dir.mkdir(parents=True, exist_ok=True)
    state_file = work_dir / "pause.json"
    resume_file = work_dir / "resume.json"

    if _run_stage(["--pause", "--state-file", str(state_file)]) != 0:
        return 1
    if _run_stage(["--resume", "--state-file", str(state_file), "--out", str(resume_file),
                   "--decision", args.decision]) != 0:
        return 1

    pause = json.loads(state_file.read_text(encoding="utf-8"))
    resume = json.loads(resume_file.read_text(encoding="utf-8"))
    problems = validate_drill_pair(pause, resume)
    ttl_report = asyncio.run(sample_checkpoint_ttl())

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": environment,
        "pause": pause,
        "resume": resume,
        "ttl": ttl_report,
        "problems": problems,
        "ok": not problems,
    }
    report_path = Path(args.report) if args.report else REPORTS_DIR / f"checkpoint-drill-{stamp}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("==== 检查点跨进程恢复演练 ====")
    print(f"  环境：{environment['redis_url']}，RediSearch={'可用' if environment['redi_search'] else '不可用'}")
    print(f"  阶段 A：thread_id={pause['thread_id']}，中断={pause['interrupted']}，"
          f"消息 {len(pause['messages'])} 条")
    print(f"  阶段 B（新进程）：检查点命中={resume['found_checkpoint']}，"
          f"审批决策={resume.get('approval')}")
    print(f"  保留策略：{ttl_report['policy']}（抽样 TTL={ttl_report['ttl_seconds']}）")
    for problem in problems:
        print(f"  [问题] {problem}")
    print(f"  结论：{'通过' if not problems else '未通过'}；报告已写入 {report_path}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())