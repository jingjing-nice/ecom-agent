# 家装智能客服与运营平台

面向简历演示的家装灯具电商业务项目：使用 LangGraph Supervisor 协调导购、售后和运营三个 Agent，LangChain 提供模型与工具协议，FastAPI 提供受控业务接口，React 提供角色工作台。订单、商品与外部售后单均为沙箱数据，不执行真实支付或退款。

## 启动

安装并启动 Docker Desktop，确保 5433、6380、7475、7688、9100、9101、8090、5173 端口可用。

```sh
cp .env.example .env
sh scripts/bootstrap.sh
```

脚本检查 Docker、等待基础设施健康、构建镜像、执行迁移、初始化数据、同步分析库、导入图谱及创建私有桶，然后启动 API、Worker 和 Web。已有业务数据不会被清空；固定演示商品、订单商品快照和政策文本会原位更新为家装灯具数据，账号、订单状态、库存及政策发布状态保留。MinIO 使用固定版本官方源码本地构建，首次启动需要下载 Go 依赖并编译；来源及许可证见 `docker/minio/Dockerfile`。

- 工作台：http://localhost:5173
- API：http://localhost:8090/docs
- MinIO 控制台：http://localhost:9101

`.env` 同时传入 API 和 Worker；容器内数据库等主机名由 Compose 覆盖。模型密钥只在服务端使用。

## 演示账号

密码均为 `Demo123!`，仅用于本地演示。

| 账号 | 权限 |
| --- | --- |
| buyer@demo.local | 买家：本人订单、售后、上传、导购、会话 |
| buyer2@demo.local | 第二位买家：验证订单及凭证隔离 |
| staff@demo.local | 客服：授权店铺工单、已分配会话 |
| ops@demo.local | 运营：授权店铺商品、订单只读查询及受控聚合分析 |
| knowledge_admin@demo.local | 知识管理员：导入、预览、审核、发布及撤销 |

另有 `@other.local` 租户账号。固定种子包含每租户两个店铺、30 个商品、90 个 SKU、12 张订单和演示政策；不把该规模描述为生产数据。

## 当前可演示流程

1. **导购**：输入“500 元以内的吸顶灯”查看可售卡片；继续问“换成 300 元以内”，继承同会话品类并修改预算。切换品类不沿用旧尺寸；新建对话清空服务端历史上下文。
2. **识图与凭证**：买家选择图片类型并上传，刷新任务状态。商品图识别结果可随消息送入导购；退款凭证可与所选订单核对，人工补录保留原值及操作者。识别不判断真伪或责任。
3. **自动售后**：加载本人订单 → 选择退款或退货退款 → 生成草稿 → 确认提交。Worker 根据已发布的结构化规则核验订单状态、支付时间、金额与所需凭证；材料不足自动提示补充，补料后重新审核，通过后自动创建沙箱售后单。CREATED 不代表退款完成。换货自动执行尚未开放。
4. **缺货协作**：选择本人 DEMO-0000 商品，目标尺寸 42，输入“换大一号，没货就推荐同品牌替代款”。售后核验订单、政策及库存后，导购保留品牌、预算和尺寸约束推荐替代款。
5. **会话记录**：买家查看历史和工具依据；当前工作台不提供转人工或客服审批入口。历史人工接口保留兼容，不是自动售后流程的依赖。
6. **运营**：查看指标字典，输入明确问题及时间范围，返回表格、可选择字段的柱状对比、口径、查询编号及同步水位。金额字段保留原单位，不自动推断换算。
7. **知识管理**：管理员导入文本或 UTF-8 文件 → 预览切片 → 刷新索引任务 → 确认审核 → 发布。重叠版本拒绝发布；撤销后停止在线引用。失败的知识索引及图片识别任务可由本人重试，外部售后写任务不开放通用重放。

完整操作和验收命令见 [演示手册](docs/demo.md)，实际验证状态见 [验证记录](docs/validation.md)。

## 模型和商品检索

无 Key 时使用规则路由、商品关键词/结构化搜索及政策关键词检索；NL2SQL、图片识别和向量检索需配置实际 Provider。

- `DEEPSEEK_API_KEY`：模型路由、工具选择、答复合成及 NL2SQL。
- `EMBEDDING_API_KEY`、`EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`：独立 Embedding 服务；`EMBEDDING_DIMENSION` 与旧名 `EMBEDDING_DIM` 均支持，前者优先。
- `DASHSCOPE_API_KEY`：视觉识别；未提供独立 Embedding 配置时兼容复用此配置。
- `AGENT_ROUTING_MODE=rules` 可在已配置 Key 时仍选择规则基线。

商品关键词和向量候选通过 RRF 融合，再经业务库核验授权范围、预算、尺寸、品牌、上下架及库存；图谱候选同样复核。向量索引不保存交易事实。初始与新增迁移固定向量维度为 1024，更换维度必须新增迁移。

配置好 Embedding 后显式构建商品向量索引（会调用 Provider）：

```sh
docker compose run --rm api python -m scripts.catalog_index --embed
```

相同内容哈希及模型版本跳过重复生成；内容更新后旧索引不再参与召回。没有向量候选时保留关键词/结构化基线，不伪造模型结果。

## 本地开发与检查

后端当前验证环境 Python 3.13，前端 Node 20.19.5，依赖分别固定于 `requirements.lock` 和 `frontend/package-lock.json`。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m scripts.seed
.venv/bin/python -m scripts.analytics_sync
.venv/bin/python -m uvicorn src.main:app --port 8090 --reload
# 另开终端
.venv/bin/python -m src.worker
# 前端
cd frontend
nvm use
npm ci
npm run dev
```

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m scripts.eval_run --check
.venv/bin/python -m alembic upgrade head --sql
npm --prefix frontend run build
```

## 实现边界

Worker 直接消费 PostgreSQL 持久化任务，通过事务锁领取，支持指数退避。Redis 用于检查点及缓存，没有启用 Celery。分析库与业务库是同一 PostgreSQL 实例的独立数据库；分析 Agent 使用只读账号及 FORCE RLS，暂未拆为独立实例。

画像小群组保护尚未交付，画像维度保持拒绝。真实平台商品页跳转、真实平台接入、完整生产权限、目标数据规模与在线模型质量验收仍待完成。LangGraph 咨询建议确认默认关闭；开启 `AGENT_HUMAN_APPROVAL` 后可在聊天中确认或取消，并通过 `/v1/chat/approve` 恢复。确认建议不替代售后提交、系统审核与状态机。

未运行的在线评测、压测及恢复演练不报告准确率或性能数字。后续工作见 [实施进度](docs/implementation.md)。


## 面试真实模型演示

已保存三条链路的真实模型实测：图片导购、售后缺货移交、运营 SQL 与基准对照，以及建单超时进入 UNKNOWN 后先核对再重试的沙箱案例。两轮同版代码实测、失败记录、模型和数据规模见 [面试演示结果](docs/interview-demo-results.md)，脱敏请求与工具轨迹见 [证据索引](docs/interview-evidence/index.json)。小样本结果不代表生产准确率或 P95。

初始化本地数据库、分析投影和对象存储后，可用自己的图片复现：

```sh
.venv-ecom/bin/python -m scripts.interview_demo --image /path/to/bathroom-light.png --expected-category 浴室灯 --vision-model qwen3-vl-plus --start-api
```

脚本创建独立沙箱租户、启动临时模型模式 API，结束后自动退出；不会调用真实退款接口。实际运行需要有效模型密钥，失败记录会保留。

## LangChain 与 LangGraph RAG

知识入库已切换为异步模型语义切片：导入原文 → LangChain Document 与官方 RecursiveCharacterTextSplitter 安全分窗 → LangGraph 调用聊天模型选择相邻原文单元分组 → 精确原文及长度校验 → GatewayEmbeddings 向量化 → pgvector 存储 → 人工预览、审核、发布。

模型只输出分组边界与标题，不重写政策原文。没有聊天密钥、边界非法或调用预算耗尽时任务失败，不回退到规则切片。缺少 Embedding 密钥时仍可完成模型切片，索引显式降级为 keyword-only。已有旧版本不会自动重切，需导入新的文档版本。

在线检索由原生 LangChain BaseRetriever 和串行 Runnable 执行：关键词检索 → 向量检索 → RRF 与版本冲突检查 → 模型选择相关原文引句 → 引用核对。固定流水线不再额外编译状态图，同一数据库会话仍串行查询。租户、政策有效期及发布状态由数据库过滤，模型不能覆盖。无依据或版本冲突不生成确定结论。

配置项：`RAG_CHUNK_MODEL` 留空复用 `CHAT_MODEL` 及聊天网关凭证；`RAG_CHUNK_WINDOW_CHARS=6000`、`RAG_CHUNK_MAX_MODEL_CALLS=40`、`RAG_INGEST_TIMEOUT_SECONDS=300`。`CHUNK_MAX_CHARS=500` 为最终单片上限。更换聊天模型不会自动更换 Embedding 模型。

本次依赖新增 `langchain-text-splitters==1.1.3`，本地需重新安装 `requirements.lock`；容器需重新构建 API 和 Worker。数据表结构没有变化。切片完成前预览片段列表为空，请刷新切片与索引任务后再审核。

## 全自动售后规则

`AFTERSALE_AUTO_REVIEW=true` 默认启用。先执行新迁移并重启 API、Worker：

```sh
.venv-ecom/bin/python -m alembic upgrade head
```

知识管理员导入售后政策时可填写结构化规则 JSON，预览后与政策版本一起发布。规则必须由经营方预先配置；客服处理期间无需人工审批。已有政策不会自动获得执行权限，需要导入带规则的新版本，撤销重叠旧版本后发布。示例：

```json
{"rules":[{"case_type":"refund","order_statuses":["paid"],"max_days_since_payment":14,"max_amount_minor":50000,"require_evidence":false}]}
```

上述示例仅授权未发货人民币订单的整条订单明细退款申请。`return_refund` 可另配规则；目前仅创建沙箱售后单，未实现真实退货验收或资金退款。`require_evidence=true` 时，原始 OCR 的订单号、明细实付金额必须同时精确匹配，买家手工补录不作为自动通过依据。模型不推断退款权限或责任归属。

系统在实际建单前再次核验订单与政策；已退款或已有执行中售后的明细不能重复申请。外部创建超时先按稳定请求号查单，有幂等保障且确认未创建时自动重试；无幂等保障时保持 UNKNOWN 并暂停写入，核对重试有次数上限。政策缺失、冲突或未配置类型进入 NEED_MORE_INFO 并说明原因，不虚构处理成功。换货、部分退款、真实平台退款完成与自动物流验收仍待实现。

## 镜像下载受限时的本地运行

`docker-compose.local.yml` 使用本机缓存的 PostgreSQL 16/pgvector、Redis 7 与 MinIO，独立命名卷，不覆盖完整版 PostgreSQL 17 数据目录；它不启动 Neo4j。普通 Redis 不提供 Redis Stack 检查点，因此此模式显式使用进程内检查点。

```sh
docker compose -p ecom-local -f docker-compose.local.yml up -d --wait
.venv-ecom/bin/python -m alembic upgrade head
.venv-ecom/bin/python -m scripts.seed
.venv-ecom/bin/python -m scripts.analytics_sync
.venv-ecom/bin/python -c 'from src.infra.storage import ensure_bucket_exists; ensure_bucket_exists()'
CHECKPOINT_BACKEND=memory .venv-ecom/bin/python -m uvicorn src.main:app --host 127.0.0.1 --port 8090
# 另开终端
.venv-ecom/bin/python -m src.worker
# 再开终端
npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173
```

按需执行 `.venv-ecom/bin/python -m scripts.catalog_index --embed` 建立商品向量索引。独立 Embedding Key 不会被当作视觉 Key 使用。当前本地模式不证明 Neo4j、Redis Stack 或视觉 OCR 已可用。

## 同一对话串联三类 Agent

使用 `ops@demo.local`（密码 `Demo123!`）登录新版聊天界面，可以在同一会话依次发送商品选购、授权店铺订单和运营指标问题，无需切换页面。Supervisor 按当前消息委派导购、订单售后或运营分析 Agent；订单卡片、统计表和图表均直接来自工具结构化结果。

示例：先问“500 元以内的吸顶灯”，再问“查询 DEMO-0000 订单的发货状态”，最后问“按品类统计 2026 年 10 月 1 日至 2026 年 11 月 1 日的支付金额”。运营账号只能查询授权租户/店铺数据，无权提交买家售后申请。买家聊天同样支持自然语言查询本人订单，但不开放运营统计。

## 家装数据更新

重新运行 `python -m scripts.seed` 更新固定演示实体，再运行 `python -m scripts.analytics_sync` 同步运营统计；使用 Neo4j 时运行 `python -m scripts.graph_seed`。商品尺寸以灯体直径（厘米）记录，兼容 API 的 `size` 字段。商品向量需按前述命令重新构建。历史面试实测记录和旧图片仅作为原始证据保留，不代表本次家装版本已完成在线模型验证。
