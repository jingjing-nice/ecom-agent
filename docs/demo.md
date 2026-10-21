# 项目演示与验收手册

本手册用于在本地沙箱复演业务闭环。演示前先完成启动和 Provider 配置；未配置模型时只演示规则与关键词基线。

## 启动后检查

```sh
sh scripts/bootstrap.sh
.venv/bin/python -m scripts.demo_smoke
```

冒烟脚本登录模拟买家、创建并取消草稿、核对持久化状态、检查商品预算及库存、检查会话历史，并验证另一买家无法读取工单。会产生少量演示记录，不会创建真实交易。该脚本必须对实际服务运行，不能用离线单测结果代替。

已有数据库升级只需执行 `docker compose run --rm api python -m alembic upgrade head`。分析同步水位随 `scripts.analytics_sync` 与投影在同一事务更新，不取 API 响应时间冒充同步时间。

## 三条面试主线

### 需求到商品

买家输入预算、品类，再连续调整预算。展示 `slot_sources` 中 message、payload、conversation、handoff 来源。商品图片需要识别完成后随消息使用；不清晰或未映射特征要保留不确定性。

配置 Embedding 并执行 `scripts.catalog_index --embed` 后，解释关键词、向量与图谱如何提供候选。打开卡片 `retrieval` 元数据查看实际召回来源。图谱仅提供关系，SKU 价格及库存来自业务库。当前使用小数据精确向量检索，未声称 ANN 大规模表现。

### 售后到沙箱工单

买家选择本人订单和目标尺寸，描述“换大一号，没货就推荐同品牌替代款”。查看订单核验、政策、库存及跨 Agent 移交工具记录。

上述换货缺货场景演示跨 Agent 咨询与推荐，不演示自动换货建单。

自动售后另选新种子中的 DEMO-0002（未发货），选择退款，生成草稿并确认提交。Worker 自动核验规则，通过后刷新直到 CREATED。再展示签收订单的退货退款：未附凭证时 NEED_MORE_INFO，上传识别完成且订单号、实付金额精确匹配的凭证后提交补料，系统自动重审。CREATED 仅表示沙箱售后单已创建，未执行真实退款或退货验收。

新种子 DEMO-0000 保留签收状态和目标 42 厘米缺货，用于跨 Agent 场景；DEMO-0002 用于自动退款。种子业务时间相对首次初始化时间，ID 与商品随机数据固定；已有数据库的固定演示商品及快照会被更新，订单时间与状态保留，较早初始化的订单可能超期。已有旧政策须先按 README 导入并发布带自动规则的新版本。

不允许取消 APPROVED、CREATING、UNKNOWN 和 CREATED，避免与外部创建竞争。UNKNOWN 必须按稳定请求号核对；不能调用通用任务重放直接再次创建。

### 问题到分析结果

运营查看指标字典，输入明确指标及时间。展示模型实际生成 SQL、AST/语义计划检查、逐表店铺过滤、只读执行、查询编号和同步水位。

最低销量问题应包含零销量基准；退款按 completed_at，支付按 paid_at。金额以分返回时保持单位，客单价分母为零返回空值。画像维度目前拒绝，不在演示中假装已提供隐私分组分析。

## 知识管理

知识管理员导入自编政策，先刷新模型切片与索引任务，完成后预览切片原文位置，确认审核后发布。发布重叠版本应失败；撤销后重新查询不得引用该版本。keyword-only 表示真实降级，不是向量构建成功。

当前买家工作台不提供转人工入口，售后处理不依赖客服审批。历史人工接口仅为兼容保留，不作为当前面试主线。

## 评测与故障演练

```sh
.venv/bin/python -m scripts.eval_run --check --report evals/reports/offline.json
.venv/bin/python -m scripts.eval_run --live --report evals/reports/live.json
.venv/bin/python -m scripts.checkpoint_drill --run
.venv/bin/python -m scripts.loadtest --help
```

在线评测需要数据库和实际模型。压测参数应按 `--help` 指定，把配置、数据规模、错误率与 P95 一起保存，不能将离线规则测试称为模型准确率。

在 `.env` 设置 `PLATFORM_FAULT_MODE=timeout`、`PLATFORM_PROBE_RESULT=created` 后重建 Worker，可演示 UNKNOWN 核对后回到 CREATED。演练后恢复 `PLATFORM_FAULT_MODE=none`。故障脚本按进程消费，重启会重置次数。

## 简历可陈述的范围

可以陈述实际实现的三 Agent 编排、受控工具、RAG 引用、混合商品检索、受控 NL2SQL、OCR 核对、状态机、幂等和沙箱恢复机制。量化指标只引用自己生成的报告，并注明数据规模和环境；不写真实业务收益、生产用户量或未验证的模型准确率。


## 模型语义切片与 RAG 验证

本轮入库使用 LangChain 文档与切分组件、LangGraph 模型分组状态图。导入接口先返回 pending 和 job_id，不同步返回切片。Worker 从原文构建片段，模型决定语义分组边界，服务端检查全文覆盖、长度及原文位置，并保存切片模型和算法版本。Embedding 失败重试时复用已保存切片。

检索状态图使用 LangChain Retriever，串行执行关键词和向量查询（避免共享数据库会话并发），融合后检查所有候选的版本冲突，再生成经过原文匹配的引句。模型不改写政策事实，也不自动认定责任。

`RAG_CHUNK_MODEL` 可指定切片聊天模型，留空复用 CHAT_MODEL；其 API Key 与 Base URL 沿用现有聊天网关。Embedding 单独使用 DASHSCOPE 配置，不能将切片模型名填到 EMBEDDING_MODEL。

## 自动售后在线验收

服务启动、迁移完成、已发布自动规则且 Worker 运行后执行：

```sh
.venv-ecom/bin/python -m scripts.demo_smoke --automatic-aftersale
```

该选项额外核验确认提交、同一幂等键重放、异步自动审核以及沙箱建单，失败或超时返回非零退出码。它会占用一个尚未申请售后的未发货订单明细；没有可用明细时明确失败，不重复创建售后单。普通冒烟不提交售后。
