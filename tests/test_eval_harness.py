"""评测装置自检（方案 §7）：数据集合规、断言引擎正确、评测器非恒真。

- 离线固定评测集必须全通过，且与基线摘要锁定一致
- 每个评测器用篡改期望的负例验证会真实判失败（防止评测装置恒真）
- 在线维度数据集 schema 校验（--live 执行，不在 CI 运行）
"""
import json
from pathlib import Path

import pytest

from scripts.eval_run import (
    BASELINE_PATH,
    LIVE_DIMENSIONS,
    OFFLINE_EVALUATORS,
    EvalResult,
    build_report,
    check_expect,
    dataset_digest,
    load_cases,
    run_offline_cases,
    verify_baseline,
)
from scripts.loadtest import percentile

DATASETS = load_cases()


def _case(case_id: str) -> dict:
    for cases in DATASETS.values():
        for case in cases:
            if case["case_id"] == case_id:
                return json.loads(json.dumps(case))
    raise AssertionError(f"评测用例不存在：{case_id}")


def test_offline_eval_set_passes_fully():
    results = run_offline_cases(DATASETS)
    assert results
    assert [r for r in results if not r.passed] == []


def test_dataset_summaries_are_locked_by_baseline():
    assert BASELINE_PATH.exists()
    digest = dataset_digest()
    locked = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["datasets"]
    assert set(locked) == set(digest)
    for name, item in locked.items():
        assert item["cases"] == digest[name]["cases"]
        assert item["sha256"] == digest[name]["sha256"]


def test_check_expect_engine_covers_paths_and_contains():
    actual = {"status": "matched", "checks": {"order_no": "matched"}, "reason": "与订单号精确一致"}
    assert check_expect({"status": "matched", "checks.order_no": "matched", "reason_contains": "精确"}, actual) == []
    failures = check_expect(
        {"status": "mismatched", "checks.amount_minor": "matched", "reason_contains": "没有"}, actual)
    assert len(failures) == 3


def test_plan_evaluator_fails_on_tampered_expectation():
    case = _case("plan-003")
    case["expect"] = {"ok": True}
    failures, _ = OFFLINE_EVALUATORS["plan_validation"](case)
    assert failures


def test_sql_benchmark_evaluator_fails_on_tampered_expectation():
    case = _case("sql-paid_amount")
    case["expect"]["contains"] = ["non_existent_token"]
    failures, _ = OFFLINE_EVALUATORS["sql_benchmark"](case)
    assert failures


def test_evidence_evaluator_fails_on_tampered_expectation():
    case = _case("ev-001")
    case["expect"]["status"] = "mismatched"
    failures, _ = OFFLINE_EVALUATORS["evidence_verification"](case)
    assert failures


def test_state_machine_evaluator_fails_on_tampered_expectation():
    case = _case("sm-001")
    case["expect"] = {"allowed": False}
    failures, _ = OFFLINE_EVALUATORS["state_machine"](case)
    assert failures


def test_knowledge_evaluator_fails_on_tampered_expectation():
    case = _case("kp-002")
    case["expect"] = {"chunk_count": 99}
    failures, _ = OFFLINE_EVALUATORS["knowledge_pipeline"](case)
    assert failures


def test_verify_baseline_detects_missing_and_changed_entries(tmp_path: Path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"version": 1, "datasets": {"a.jsonl": {"sha256": "old", "cases": 1}}}),
                        encoding="utf-8")
    changed = verify_baseline({"a.jsonl": {"sha256": "new", "cases": 1}}, baseline)
    assert any("摘要与基线不一致" in item for item in changed)
    missing = verify_baseline({"b.jsonl": {"sha256": "n", "cases": 1}}, baseline)
    assert any("未在基线中锁定" in item for item in missing)
    assert any("数据集缺失" in item for item in missing)
    assert verify_baseline(dataset_digest()) == []


def test_build_report_summarizes_results_and_skips():
    results = [EvalResult("a", "d1", True, "", 1), EvalResult("b", "d1", False, "x", 1),
               EvalResult("c", "d2", True, "", 1)]
    report = build_report(results, mode="offline", skipped=["routing（--live）"])
    assert report["summary"]["total"] == 3
    assert report["summary"]["passed"] == 2
    assert report["summary"]["failed"] == 1
    assert report["dimensions"]["d1"] == {"total": 2, "passed": 1, "failed": 1}
    assert report["skipped"] == ["routing（--live）"]


def test_live_datasets_schema_is_complete():
    from src.modules.analytics.benchmarks import BENCHMARKS

    routing = DATASETS["routing.jsonl"]
    assert routing and all(c["role"] in {"buyer", "ops"} and c["message"] and c["expect"]["agent"]
                           for c in routing)
    grounding = DATASETS["policy_grounding.jsonl"]
    assert grounding and all(isinstance(c["expect"]["refusal"], bool) for c in grounding)
    compare = DATASETS["sql_result_compare.jsonl"]
    assert compare and all(c["question"] and c["metric_id"] in BENCHMARKS for c in compare)
    offline_in_live_files = {*map(lambda name: Path(name).stem, DATASETS)} & set(LIVE_DIMENSIONS)
    assert offline_in_live_files == set(LIVE_DIMENSIONS)


def test_loadtest_percentile_helper():
    assert percentile([], 0.5) is None
    assert percentile([10, 20, 30, 40], 0.5) == 30
    assert percentile([10, 20, 30, 40], 0.95) == 40
    assert percentile([7], 0.99) == 7