"""真实 API 压测脚本（方案 §7）。

面向已启动的 API 服务（python -m uvicorn src.main:app）执行并发聊天请求，
统计成功率、错误分类与 P50/P95/P99 延迟，产出 JSON 报告供容量验证引用。

- 数据集复用固定评测集（evals/cases/routing.jsonl）的问题文本，避免口径漂移
- 每个请求独立会话；client_message_id 随机，不触发幂等重放
- 只有真实运行时环境才产生报告；脚本不伪造延迟数据。数据库、模型与
  Worker 未就绪时错误会计入 http/stream 分类并如实报告

用法：
  python scripts/loadtest.py --base-url http://127.0.0.1:8090 --concurrency 8 --requests 64
  python scripts/loadtest.py --role ops --concurrency 4 --requests 32
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORTS_DIR = ROOT / "evals" / "reports"
ROUTING_CASES = ROOT / "evals" / "cases" / "routing.jsonl"

DEMO_ACCOUNTS = {"buyer": "buyer@demo.local", "ops": "ops@demo.local"}
DEMO_PASSWORD = "Demo123!"
FALLBACK_MESSAGES = ["你好，帮我看看商品", "这盏灯尺寸太小了怎么办"]


def load_messages(role: str) -> list[str]:
    if not ROUTING_CASES.exists():
        return list(FALLBACK_MESSAGES)
    messages = []
    for line in ROUTING_CASES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        if case.get("role", "buyer") == role:
            messages.append(case["message"])
    return messages or list(FALLBACK_MESSAGES)


def git_head() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


async def one_request(client: httpx.AsyncClient, base_url: str, token: str, message: str,
                      *, order_item_id: str | None, timeout: float) -> dict:
    """单次聊天请求：新建会话 → SSE 到 answer.completed；返回耗时与结局。"""
    started = time.perf_counter()
    headers = {"Authorization": f"Bearer {token}"}
    outcome = {"ok": False, "error": None, "latency_ms": None}
    try:
        conv = await client.post(f"{base_url}/v1/conversations", headers=headers)
        if conv.status_code != 200:
            outcome["error"] = f"http_{conv.status_code}"
            return outcome
        payload = {"conversation_id": conv.json()["data"]["id"], "message": message,
                   "client_message_id": uuid4().hex}
        if order_item_id:
            payload["order_item_id"] = order_item_id
        event = None
        answer = error = None
        async with client.stream("POST", f"{base_url}/v1/chat/stream", json=payload,
                                 headers=headers, timeout=timeout) as response:
            if response.status_code != 200:
                await response.aread()
                outcome["error"] = f"http_{response.status_code}"
                return outcome
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    event = line[7:].strip()
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                    if event == "answer.completed":
                        answer = data
                    elif event == "error":
                        error = data
        if answer is not None:
            outcome["ok"] = True
        else:
            outcome["error"] = f"stream_error:{(error or {}).get('error_code', 'no_answer')}"
    except httpx.TimeoutException:
        outcome["error"] = "timeout"
    except httpx.HTTPError as exc:
        outcome["error"] = f"http_error:{type(exc).__name__}"
    finally:
        outcome["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return outcome


def percentile(values: list[int], ratio: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(int(len(ordered) * ratio), len(ordered) - 1)
    return ordered[index]


async def run_loadtest(base_url: str, *, role: str, concurrency: int, requests: int,
                       timeout: float, warmup: int) -> dict:
    base_url = base_url.rstrip("/")
    messages = load_messages(role)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        login = await client.post(f"{base_url}/v1/auth/login",
                                  json={"email": DEMO_ACCOUNTS[role], "password": DEMO_PASSWORD})
        login.raise_for_status()
        token = login.json()["data"]["access_token"]

        order_item_id = None
        if role == "buyer":
            orders = await client.get(f"{base_url}/v1/orders", params={"limit": 1},
                                      headers={"Authorization": f"Bearer {token}"})
            if orders.status_code == 200 and orders.json()["data"]["items"]:
                items = orders.json()["data"]["items"][0].get("items") or []
                order_item_id = items[0]["id"] if items else None

        for index in range(warmup):
            await one_request(client, base_url, token, messages[index % len(messages)],
                              order_item_id=order_item_id, timeout=timeout)

        semaphore = asyncio.Semaphore(concurrency)

        async def worker(index: int) -> dict:
            async with semaphore:
                return await one_request(client, base_url, token, messages[index % len(messages)],
                                         order_item_id=order_item_id, timeout=timeout)

        started = time.perf_counter()
        outcomes = await asyncio.gather(*(worker(i) for i in range(requests)))
        wall_seconds = time.perf_counter() - started

    latencies = [item["latency_ms"] for item in outcomes if item["ok"]]
    errors: dict[str, int] = {}
    for item in outcomes:
        if not item["ok"]:
            errors[item["error"] or "unknown"] = errors.get(item["error"] or "unknown", 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"base_url": base_url, "role": role, "git_head": git_head(),
                        "python": sys.version.split()[0]},
        "params": {"concurrency": concurrency, "requests": requests, "timeout_s": timeout,
                   "warmup": warmup},
        "summary": {
            "ok": len(latencies),
            "failed": len(outcomes) - len(latencies),
            "success_rate": round(len(latencies) / len(outcomes), 4) if outcomes else None,
            "wall_seconds": round(wall_seconds, 3),
            "rps": round(len(outcomes) / wall_seconds, 2) if wall_seconds else None,
            "latency_ms": {
                "min": min(latencies) if latencies else None,
                "p50": percentile(latencies, 0.50),
                "p95": percentile(latencies, 0.95),
                "p99": percentile(latencies, 0.99),
                "max": max(latencies) if latencies else None,
                "mean": int(statistics.mean(latencies)) if latencies else None,
            },
            "errors": errors,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实 API 压测（方案 §7）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--role", choices=["buyer", "ops"], default="buyer")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--requests", type=int, default=64)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--report", help="报告输出路径（默认写入 evals/reports/）")
    args = parser.parse_args(argv)

    report = asyncio.run(run_loadtest(args.base_url, role=args.role, concurrency=args.concurrency,
                                      requests=args.requests, timeout=args.timeout, warmup=args.warmup))
    summary = report["summary"]
    latency = summary["latency_ms"]
    print(f"==== 压测结果（{args.base_url}，并发 {args.concurrency}，请求 {args.requests}）====")
    print(f"  成功 {summary['ok']}/{summary['ok'] + summary['failed']}"
          f"（成功率 {summary['success_rate']}），RPS {summary['rps']}")
    print(f"  延迟 P50={latency['p50']}ms P95={latency['p95']}ms P99={latency['p99']}ms"
          f" max={latency['max']}ms")
    if summary["errors"]:
        print(f"  失败分类：{summary['errors']}")
    print("  备注：结果仅代表本次环境的实测值；镜像与数据集未被锁定时不对外引用。")

    report_path = Path(args.report) if args.report else (
        REPORTS_DIR / f"loadtest-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  报告已写入 {report_path}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())