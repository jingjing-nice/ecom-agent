"""外部售后平台适配契约与故障注入（方案 §5）。

适配器只创建本地演示单号，不访问真实电商平台。契约三态：
- create_case → created / timeout / unknown（timeout 与 unknown 都表示结果未知）
- query_case → created / missing / timeout（按稳定请求号核对外部状态）

稳定请求号 REQ-{case uuid hex} 由工单派生、重试期间不变，作为平台幂等键；
无幂等平台（supports_idempotency=False）在 UNKNOWN 后禁止自动重试创建，
必须由人工核对外部平台后重新审批（人工核对分支）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AdapterResult:
    """适配器调用结果；external_case_id 仅在 created 时存在。"""

    status: str
    external_case_id: str | None = None
    detail: str = ""


class PlatformAdapter(Protocol):
    name: str
    supports_idempotency: bool

    async def create_case(self, *, request_no: str, case_id: str) -> AdapterResult: ...

    async def query_case(self, *, request_no: str) -> AdapterResult: ...


def stable_request_no(case) -> str:
    """稳定请求号：由工单 UUID 派生，重试与崩溃恢复期间不变化。"""
    return "REQ-" + case.id.hex


def plan_after_create(status: str, *, supports_idempotency: bool) -> dict:
    """create 结果处置计划。

    超时或结果未知：工单置 UNKNOWN，凭稳定请求号核对；
    非成功结果一律进入重试路径（核对后决定 CREATED 或人工分支）。
    """
    if status == "created":
        return {"case_status": "CREATED", "retry": False, "reason": ""}
    if not supports_idempotency:
        return {"case_status": "UNKNOWN", "retry": True,
                "reason": "外部平台不支持幂等请求号，创建结果未知，需人工核对"}
    return {"case_status": "UNKNOWN", "retry": True, "reason": "外部平台创建结果未知，将按稳定请求号核对"}


def plan_after_probe(status: str, *, supports_idempotency: bool) -> dict:
    """UNKNOWN 核对结果处置计划。

    核对到已创建 → CREATED；核对不存在 → 退回 REVIEWING（重新审批）；
    有幂等保障时自动重试安全，无幂等平台必须人工核对后重新审批。
    """
    if status == "created":
        return {"case_status": "CREATED", "retry": False, "reason": "核对发现外部单已创建"}
    if status == "missing":
        reason = ("核对未发现外部单，退回重新审批" if supports_idempotency
                  else "平台不支持幂等请求号：请人工核对外部平台后重新审批")
        return {"case_status": "REVIEWING", "retry": False, "reason": reason}
    return {"case_status": "UNKNOWN", "retry": True, "reason": "外部状态查询超时，稍后重试核对"}


class SandboxPlatformAdapter:
    """本地演示适配器：稳定请求号 → 同一沙箱单号（幂等），不访问网络。"""

    name = "sandbox"
    supports_idempotency = True

    async def create_case(self, *, request_no: str, case_id: str) -> AdapterResult:
        return AdapterResult("created", external_case_id="SANDBOX-" + case_id)

    async def query_case(self, *, request_no: str) -> AdapterResult:
        # 沙箱不持久化外部单：演示环境按未创建处理，走人工核对分支。
        return AdapterResult("missing", detail="沙箱适配器不保存外部单")


class FaultInjectingAdapter:
    """在沙箱适配器上按脚本注入超时/UNKNOWN 故障（真实平台契约演练）。

    faults 逐次弹出（进程内单例，跨任务轮次保持）；用尽后透传沙箱行为。
    query_case 按 probe_result 返回核对结局，覆盖「核对到创建」与「人工核对分支」。
    """

    def __init__(self, base: SandboxPlatformAdapter, *, faults: list[str],
                 probe_result: str = "missing", supports_idempotency: bool = True):
        self.base = base
        self.name = f"fault:{base.name}"
        self.faults = list(faults)
        self.probe_result = probe_result
        self.supports_idempotency = supports_idempotency

    async def create_case(self, *, request_no: str, case_id: str) -> AdapterResult:
        fault = self.faults.pop(0) if self.faults else None
        if fault == "timeout":
            return AdapterResult("timeout", detail="外部平台创建超时，创建结果未知")
        if fault == "unknown":
            return AdapterResult("unknown", detail="外部平台未能确认创建结果")
        return await self.base.create_case(request_no=request_no, case_id=case_id)

    async def query_case(self, *, request_no: str) -> AdapterResult:
        if self.probe_result == "created":
            return AdapterResult("created", external_case_id="SANDBOX-" + request_no.removeprefix("REQ-"),
                                 detail="核对发现外部单已创建")
        if self.probe_result == "timeout":
            return AdapterResult("timeout", detail="外部状态查询超时")
        return AdapterResult("missing", detail="核对未发现外部单")


_adapter: PlatformAdapter | None = None


def build_platform_adapter(settings) -> PlatformAdapter:
    """按配置构建适配器；进程内单例保证故障脚本跨任务轮次生效。"""
    base = SandboxPlatformAdapter()
    if settings.PLATFORM_FAULT_MODE == "none":
        return base
    return FaultInjectingAdapter(
        base,
        faults=[settings.PLATFORM_FAULT_MODE] * settings.PLATFORM_FAULT_TIMES,
        probe_result=settings.PLATFORM_PROBE_RESULT,
        supports_idempotency=settings.PLATFORM_SUPPORTS_IDEMPOTENCY,
    )


def get_platform_adapter() -> PlatformAdapter:
    global _adapter
    if _adapter is None:
        from src.core.config import get_settings
        _adapter = build_platform_adapter(get_settings())
    return _adapter


def reset_platform_adapter() -> None:
    """测试/演练辅助：清除进程内适配器单例。"""
    global _adapter
    _adapter = None