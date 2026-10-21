"""固定评测集执行器（方案 §7）。

离线维度（确定性组件，CI 可执行，无模型密钥与数据库依赖）：
- plan_validation：语义计划校验（指标混算 / 画像维度 / 过滤白名单 / UTC 半开区间）
- sql_benchmark：10 个人工基准 SQL 的硬校验与口径要点（支付按 paid_at、退款按 completed_at 等）
- evidence_verification：OCR 金额/订单号精确核对、人工补录留痕与商品词表映射
- state_machine：售后状态迁移白名单
- knowledge_pipeline：知识切分定位不丢字与发布阻断条件

在线维度（--live，需要真实环境：API 服务 + 模型密钥 + PostgreSQL）：
- routing：意图路由（answer.agent 与期望 Agent 对照）
- policy_grounding：无依据拒答（无政策证据时必须声明需客服核验）
- sql_result_compare：模型生成 SQL 与人工基准 SQL 的同环境执行结果对照

用法：
  python scripts/eval_run.py                     # 离线评测，失败退出码非 0
  python scripts/eval_run.py --check             # CI：离线全通过 + 数据集摘要与基线锁定一致
  python scripts/eval_run.py --update-baseline   # 数据集变更后显式重新锁定摘要与样本数
  python scripts/eval_run.py --live --base-url http://127.0.0.1:8090

本脚本只报告实测结果，不承诺质量指标；真实环境未执行前在线维度保持 skipped。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CASES_DIR = ROOT / "evals" / "cases"
BASELINE_PATH = ROOT / "evals" / "baseline.json"
REPORTS_DIR = ROOT / "evals" / "reports"

OFFLINE_DIMENSIONS = (
    "plan_validation",
    "sql_benchmark",
    "evidence_verification",
    "state_machine",
    "knowledge_pipeline",
)
LIVE_DIMENSIONS = ("routing", "policy_grounding", "sql_result_compare")

# 售后无依据拒答判定（子 Agent 确定性文案口径）
REFUSAL_MARKERS = ("缺少有效政策依据", "需要客服核验", "没有找到", "无法确认", "转人工")
GROUNDED_MARKER = "已找到政策参考"

DEMO_ACCOUNTS = {"buyer": "buyer@demo.local", "ops": "ops@demo.local"}
DEMO_PASSWORD = "Demo123!"


@dataclass
class EvalResult:
    case_id: str
    dimension: str
    passed: bool
    detail: str = ""
    duration_ms: int = 0


@dataclass
class EvalCase:
    case_id: str
    dimension: str
    raw: dict = field(default_factory=dict)


def load_cases(cases_dir: Path = CASES_DIR) -> dict[str, list[dict]]:
    """按数据集文件加载全部固定评测用例；校验 case_id 全局唯一。"""
    datasets: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for path in sorted(cases_dir.glob("*.jsonl")):
        cases: list[dict] = []
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name} 第 {lineno} 行不是合法 JSON：{exc}") from exc
            for key in ("case_id", "dimension"):
                if key not in case:
                    raise ValueError(f"{path.name} 第 {lineno} 行缺少字段 {key}")
            if case["dimension"] in OFFLINE_DIMENSIONS and "input" not in case:
                raise ValueError(f"{path.name} 第 {lineno} 行缺少字段 input")
            if case["case_id"] in seen:
                raise ValueError(f"case_id 重复：{case['case_id']}")
            seen.add(case["case_id"])
            cases.append(case)
        if cases:
            datasets[path.name] = cases
    if not datasets:
        raise ValueError(f"评测集目录为空：{cases_dir}")
    return datasets


def dataset_digest(cases_dir: Path = CASES_DIR) -> dict[str, dict]:
    """数据集摘要锁定：逐文件 sha256 与样本数。"""
    digest: dict[str, dict] = {}
    for path in sorted(cases_dir.glob("*.jsonl")):
        content = path.read_bytes()
        cases = load_cases(cases_dir)[path.name]
        digest[path.name] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "cases": len(cases),
            "mode": "offline" if path.stem in OFFLINE_DIMENSIONS else "live",
        }
    return digest


def verify_baseline(digest: dict[str, dict], baseline_path: Path = BASELINE_PATH) -> list[str]:
    """核对数据集与基线锁定一致；返回不一致说明（空表示通过）。"""
    if not baseline_path.exists():
        return [f"缺少基线文件 {baseline_path.name}，请先执行 --update-baseline"]
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    locked = baseline.get("datasets", {})
    problems: list[str] = []
    for name, current in digest.items():
        locked_item = locked.get(name)
        if locked_item is None:
            problems.append(f"{name} 未在基线中锁定")
        elif locked_item["sha256"] != current["sha256"]:
            problems.append(f"{name} 摘要与基线不一致（数据集已变更，需显式重新锁定）")
    for name in locked:
        if name not in digest:
            problems.append(f"{name} 基线存在但数据集缺失")
    return problems


def write_baseline(digest: dict[str, dict], baseline_path: Path = BASELINE_PATH) -> None:
    payload = {"version": 1, "locked_at": datetime.now(timezone.utc).isoformat(), "datasets": digest}
    baseline_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 期望断言引擎：点路径取值 + *_contains 包含断言
# ---------------------------------------------------------------------------


def _dig(actual, key: str):
    node = actual
    for part in key.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def check_expect(expect: dict, actual: dict) -> list[str]:
    """逐键比对期望与实际；返回失败说明列表。"""
    failures: list[str] = []
    for key, expected in expect.items():
        if key.endswith("_contains"):
            base = key[: -len("_contains")]
            target = actual.get(base)
            if isinstance(target, (dict, list)):
                target = json.dumps(target, ensure_ascii=False)
            if not isinstance(target, str) or str(expected) not in target:
                failures.append(f"{base} 应包含 {expected!r}，实际 {target!r}")
        else:
            got = _dig(actual, key)
            if got != expected:
                failures.append(f"{key} 期望 {expected!r}，实际 {got!r}")
    return failures


# ---------------------------------------------------------------------------
# 离线维度评测器：返回 (失败说明列表, 明细)
# ---------------------------------------------------------------------------


def eval_plan_validation(case: dict) -> tuple[list[str], str]:
    from src.nl2sql.plan import validate_semantic_plan

    result = validate_semantic_plan(case["input"]["plan"])
    actual = {"ok": result.ok, "reason": result.reason, "normalized_plan": result.normalized_plan}
    detail = "ok" if result.ok else f"拒绝：{result.reason}"
    return check_expect(case["expect"], actual), detail


def eval_sql_benchmark(case: dict) -> tuple[list[str], str]:
    from src.modules.analytics.benchmarks import benchmark_for
    from src.nl2sql.validator import validate_sql

    benchmark = benchmark_for(case["input"]["metric_id"])
    if benchmark is None:
        return [f"缺少指标基准：{case['input']['metric_id']}"], ""
    validation = validate_sql(benchmark.sql)
    failures: list[str] = []
    for token in case["expect"].get("contains", []):
        if token not in benchmark.sql:
            failures.append(f"基准 SQL 应包含 {token!r}")
    for token in case["expect"].get("not_contains", []):
        if token in benchmark.sql:
            failures.append(f"基准 SQL 不应出现 {token!r}")
    rest = {k: v for k, v in case["expect"].items() if k not in {"contains", "not_contains"}}
    failures += check_expect(rest, {"valid": validation.ok, "reason": validation.reason})
    return failures, f"validate_sql={'通过' if validation.ok else '拒绝'}"


def eval_evidence_verification(case: dict) -> tuple[list[str], str]:
    from src.core.exceptions import AppError
    from src.services.vision import apply_manual_field, apply_product_vocab, verify_evidence

    inp, expect = case["input"], case["expect"]
    kind = inp["kind"]
    if kind == "verify":
        result = verify_evidence({"fields": inp["fields"]}, order_no=inp.get("order_no"),
                                 amount_minor=inp.get("amount_minor"))
        actual = {**result, "checks": {item["field_name"]: item["verification"] for item in result["checks"]}}
        detail = f"status={result['status']}"
    elif kind == "manual":
        manual = inp["manual"]
        try:
            updated = apply_manual_field({"fields": inp["fields"]}, field_name=manual["field_name"],
                                         value=manual["value"], by=manual["by"])
            updated_field = next(f for f in updated["fields"] if f["field_name"] == manual["field_name"])
            actual = {**updated_field}
            detail = f"source={updated_field.get('source')}"
        except AppError as exc:
            actual = {"error": exc.message}
            detail = exc.message
    elif kind == "vocab":
        actual = apply_product_vocab(inp["recognition"])
        detail = f"unmapped={actual.get('unmapped_fields')}"
    else:
        return [f"未知 kind：{kind}"], ""
    return check_expect(expect, actual), detail


def eval_state_machine(case: dict) -> tuple[list[str], str]:
    from src.modules.aftersale.state_machine import can_transition

    inp = case["input"]
    actual = {"allowed": can_transition(inp["from"], inp["to"])}
    return check_expect(case["expect"], actual), f"{inp['from']}→{inp['to']}"


def eval_knowledge_pipeline(case: dict) -> tuple[list[str], str]:
    from src.services.knowledge import publish_blockers, split_text

    inp = case["input"]
    if inp["kind"] == "split":
        content = inp["content"]
        chunks = split_text(content, max_chars=inp["max_chars"])
        unit_coverage = all(
            chunk["content"] == content[chunk["char_start"]:chunk["char_end"]] for chunk in chunks
        )
        contiguous = [chunk["chunk_index"] for chunk in chunks] == list(range(len(chunks)))
        actual = {
            "chunk_count": len(chunks),
            "unit_coverage": unit_coverage,
            "contiguous": contiguous,
            "section_titles": [chunk["section_title"] for chunk in chunks],
        }
        detail = f"chunks={len(chunks)}"
    else:
        version = SimpleNamespace(status=inp["status"], index_version=inp["index_version"])
        chunks = [SimpleNamespace(embedding=c["embedding"], model_version=c["model_version"])
                  for c in inp["chunks"]]
        actual = {"blocker": publish_blockers(version, chunks)}
        detail = actual["blocker"] or "可发布"
    return check_expect(case["expect"], actual), detail


OFFLINE_EVALUATORS = {
    "plan_validation": eval_plan_validation,
    "sql_benchmark": eval_sql_benchmark,
    "evidence_verification": eval_evidence_verification,
    "state_machine": eval_state_machine,
    "knowledge_pipeline": eval_knowledge_pipeline,
}


def run_offline_cases(datasets: dict[str, list[dict]], *,
                      only: str | None = None) -> list[EvalResult]:
    results: list[EvalResult] = []
    for name, cases in datasets.items():
        dimension = Path(name).stem
        if dimension not in OFFLINE_DIMENSIONS or (only and dimension != only):
            continue
        evaluator = OFFLINE_EVALUATORS[dimension]
        for case in cases:
            started = time.perf_counter()
            try:
                failures, detail = evaluator(case)
            except Exception as exc:  # 评测装置异常按失败报告，不中断其余用例
                failures, detail = [f"评测器异常：{type(exc).__name__}: {exc}"], ""
            results.append(EvalResult(case["case_id"], dimension, not failures,
                                      detail if not failures else "；".join(failures),
                                      int((time.perf_counter() - started) * 1000)))
    return results


# ---------------------------------------------------------------------------
# 在线维度：HTTP 会话（routing / policy_grounding）与进程内对照（sql_result_compare）
# ---------------------------------------------------------------------------


class EvalApi:
    """真实 API 的评测会话：登录、取订单明细、读取 SSE 答案。"""

    def __init__(self, base_url: str, *, timeout: float = 120.0):
        import httpx

        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        self._tokens: dict[str, str] = {}

    async def aclose(self) -> None:
        await self.http.aclose()

    async def token_for(self, role: str) -> str:
        if role in self._tokens:
            return self._tokens[role]
        response = await self.http.post(f"{self.base_url}/v1/auth/login",
                                        json={"email": DEMO_ACCOUNTS[role], "password": DEMO_PASSWORD})
        response.raise_for_status()
        token = response.json()["data"]["access_token"]
        self._tokens[role] = token
        return token

    async def first_order_item(self, token: str) -> str | None:
        response = await self.http.get(f"{self.base_url}/v1/orders",
                                       params={"limit": 1}, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        items = response.json()["data"]["items"]
        if not items or not items[0].get("items"):
            return None
        return items[0]["items"][0]["id"]

    async def chat(self, token: str, message: str, *, order_item_id: str | None = None):
        """发送一条消息并等待 answer.completed；返回 (answer, error)。"""
        headers = {"Authorization": f"Bearer {token}"}
        conv = await self.http.post(f"{self.base_url}/v1/conversations", headers=headers)
        conv.raise_for_status()
        payload = {"conversation_id": conv.json()["data"]["id"], "message": message,
                   "client_message_id": uuid4().hex}
        if order_item_id:
            payload["order_item_id"] = order_item_id
        answer = error = None
        event = None
        async with self.http.stream("POST", f"{self.base_url}/v1/chat/stream", json=payload,
                                    headers=headers) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode("utf-8", "replace")[:200]
                raise RuntimeError(f"chat/stream HTTP {response.status_code}：{body}")
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    event = line[7:].strip()
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                    if event == "answer.completed":
                        answer = data
                    elif event == "error":
                        error = data
        return answer, error


def _grounding_state(answer: dict) -> dict:
    message = answer.get("message", "")
    grounded = GROUNDED_MARKER in message
    refused = not grounded and (any(marker in message for marker in REFUSAL_MARKERS)
                                or not answer.get("evidence"))
    return {"refusal": refused, "grounded": grounded}


async def eval_routing_live(case: dict, api: EvalApi) -> tuple[list[str], str]:
    role = case.get("role", "buyer")
    token = await api.token_for(role)
    order_item_id = await api.first_order_item(token) if role == "buyer" else None
    answer, error = await api.chat(token, case["message"], order_item_id=order_item_id)
    if answer is None:
        return [f"未收到 answer.completed：{error}"], "error"
    actual = {"agent": answer.get("agent"), "message": answer.get("message", "")}
    failures = check_expect({"agent": case["expect"]["agent"]}, actual)
    return failures, f"agent={answer.get('agent')} routing_mode={answer.get('routing_mode')}"


async def eval_policy_grounding_live(case: dict, api: EvalApi) -> tuple[list[str], str]:
    token = await api.token_for(case.get("role", "buyer"))
    order_item_id = await api.first_order_item(token)
    answer, error = await api.chat(token, case["message"], order_item_id=order_item_id)
    if answer is None:
        return [f"未收到 answer.completed：{error}"], "error"
    state = _grounding_state(answer)
    expect = {"refusal": case["expect"]["refusal"]}
    if "agent" in case["expect"]:
        expect["agent"] = case["expect"]["agent"]
    failures = check_expect(expect, {**state, "agent": answer.get("agent")})
    return failures, f"refusal={state['refusal']} 证据条数={len(answer.get('evidence', []))}"


async def eval_sql_result_compare_live(case: dict, ctx: dict) -> tuple[list[str], str]:
    from src.core.deps import ActorScope
    from src.modules.analytics.benchmarks import benchmark_for
    from src.nl2sql.executor import execute_readonly
    from src.nl2sql.generator import generate_sql

    actor = ActorScope(user_id="eval-ops", tenant_id=ctx["tenant"], role="ops",
                       shop_ids=ctx["shop_ids"])
    generated = await generate_sql(case["question"], actor=actor,
                                   semantic_plan=case.get("semantic_plan"))
    if not generated.ok:
        return [f"生成失败：{generated.error}"], ""
    baseline = benchmark_for(case["metric_id"])
    expected = await execute_readonly(baseline.sql, actor=actor)
    actual = await execute_readonly(generated.sql, actor=actor)

    def rows_key(rows):
        return sorted(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) for row in rows)

    detail = (f"生成行数={actual.row_count} 基准行数={expected.row_count} "
              f"model_calls={generated.model_calls}")
    if rows_key(actual.rows) != rows_key(expected.rows):
        return [f"执行结果与人工基准不一致：{detail}"], detail
    return [], detail


async def run_live_cases(datasets: dict[str, list[dict]], *, base_url: str,
                         tenant: str = "demo", shop_ids: list[str] | None = None,
                         only: str | None = None) -> list[EvalResult]:
    results: list[EvalResult] = []
    ctx = {"tenant": tenant, "shop_ids": shop_ids or ["shop-a", "shop-b"]}
    needs_api = [name for name in datasets if Path(name).stem in {"routing", "policy_grounding"}]
    api = EvalApi(base_url) if needs_api else None
    try:
        for name, cases in datasets.items():
            dimension = Path(name).stem
            if dimension not in LIVE_DIMENSIONS or (only and dimension != only):
                continue
            for case in cases:
                started = time.perf_counter()
                try:
                    if dimension == "routing":
                        failures, detail = await eval_routing_live(case, api)
                    elif dimension == "policy_grounding":
                        failures, detail = await eval_policy_grounding_live(case, api)
                    else:
                        failures, detail = await eval_sql_result_compare_live(case, ctx)
                except Exception as exc:
                    failures, detail = [f"评测执行异常：{type(exc).__name__}: {exc}"], ""
                results.append(EvalResult(case["case_id"], dimension, not failures,
                                          detail if not failures else "；".join(failures),
                                          int((time.perf_counter() - started) * 1000)))
    finally:
        if api is not None:
            await api.aclose()
    return results


# ---------------------------------------------------------------------------
# 报告与命令行
# ---------------------------------------------------------------------------


def build_report(results: list[EvalResult], *, mode: str, skipped: list[str] | None = None) -> dict:
    dimensions: dict[str, dict] = {}
    for item in results:
        bucket = dimensions.setdefault(item.dimension, {"total": 0, "passed": 0, "failed": 0})
        bucket["total"] += 1
        bucket["passed" if item.passed else "failed"] += 1
    passed = sum(1 for item in results if item.passed)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "pass_rate": round(passed / len(results), 4) if results else None,
        },
        "dimensions": dimensions,
        "results": [vars(item) for item in results],
        "skipped": skipped or [],
    }


def print_summary(report: dict) -> None:
    print(f"—— 评测报告（{report['mode']}，{report['generated_at']}）——")
    for dimension, bucket in sorted(report["dimensions"].items()):
        mark = "PASS" if bucket["failed"] == 0 else "FAIL"
        print(f"  [{mark}] {dimension:<24} {bucket['passed']}/{bucket['total']}")
    for item in report["results"]:
        if not item["passed"]:
            print(f"    - 失败 {item['case_id']}（{item['dimension']}）：{item['detail']}")
    for note in report["skipped"]:
        print(f"  [SKIP] {note}")
    summary = report["summary"]
    print(f"  合计 {summary['passed']}/{summary['total']}，通过率 {summary['pass_rate']}")


def run_offline(datasets: dict[str, list[dict]], *, only: str | None = None) -> dict:
    results = run_offline_cases(datasets, only=only)
    skipped = [f"{name}（--live 在线维度，需真实环境）" for name in LIVE_DIMENSIONS]
    return build_report(results, mode="offline", skipped=skipped)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="固定评测集执行器（方案 §7）")
    parser.add_argument("--live", action="store_true", help="执行在线维度（需 API + 模型密钥 + 数据库）")
    parser.add_argument("--check", action="store_true", help="CI 门槛：离线全通过 + 数据集摘要锁定一致")
    parser.add_argument("--update-baseline", action="store_true", help="显式重新锁定数据集摘要与样本数")
    parser.add_argument("--dimension", help="只执行指定维度")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090", help="--live 的 API 地址")
    parser.add_argument("--tenant", default="demo", help="sql_result_compare 的租户")
    parser.add_argument("--shop-ids", default="shop-a,shop-b", help="sql_result_compare 的店铺授权")
    parser.add_argument("--report", help="额外写出 JSON 报告路径")
    args = parser.parse_args(argv)

    datasets = load_cases()
    digest = dataset_digest()
    exit_code = 0

    if args.update_baseline:
        write_baseline(digest)
        print(f"已重新锁定 {len(digest)} 个数据集：{BASELINE_PATH}")
        return 0

    if not args.live and args.check:
        problems = verify_baseline(digest)
        if problems:
            for problem in problems:
                print(f"[基线] {problem}")
            return 1
        print(f"[基线] {len(digest)} 个数据集摘要与样本数锁定一致")

    if args.live:
        report = asyncio.run(run_live_cases(
            datasets, base_url=args.base_url, tenant=args.tenant,
            shop_ids=[s for s in args.shop_ids.split(",") if s], only=args.dimension))
    else:
        report = run_offline(datasets, only=args.dimension)
        if report["summary"]["failed"]:
            exit_code = 1

    if args.check and report["summary"]["failed"]:
        exit_code = 1
    print_summary(report)

    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"报告已写入 {path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())