# 面试真实模型演示结果

运行编号：20261018T074553Z-bc909c

三条模型链路使用真实模型和本地沙箱业务数据；恢复案例使用真实数据库与可控适配器故障，不代表真实商家接口。

| 场景 | 结果 | 耗时 ms |
| --- | --- | --- |
| image_shopping | PASS | 14165 |
| aftersale_handoff | PASS | 11677 |
| analytics_sql | PASS | 8224 |
| unauthorized_request | PASS | 2044 |
| timeout_recovery | PASS | 6011 |
| non_idempotent_safety | PASS | 1254 |

完整请求、事件、工具轨迹、SQL 对照、识别结果和恢复状态见同目录 JSON。失败记录保留，不作为成功重跑删除。

本次样本量很小，只说明这些场景的行为与耗时，不代表总体准确率或生产 P95。
