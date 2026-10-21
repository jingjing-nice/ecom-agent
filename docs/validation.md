# 本轮验证记录

验证日期：2026 年 10 月 17 日。环境：macOS ARM64、Python 3.13.16、Node 20.19.5。下面只记录本次实际执行结果。

| 检查 | 结果 |
| --- | --- |
| `python -m pytest -q` | 229 passed（含代码优化、网关与流式、语义计划与基准、召回融合、OCR 核对、知识导入、工单故障注入、评测装置与检查点演练回归） |
| `python -m compileall -q src scripts migrations` | 通过 |
| SQLAlchemy ORM `configure_mappers()` | 通过 |
| `python -m alembic upgrade head --sql` | 离线 SQL 生成通过 |
| `docker compose --profile app config --quiet` | 配置解析通过 |
| `sh -n scripts/bootstrap.sh` | 语法检查通过 |
| `python scripts/eval_run.py --check` | 离线维度 61/61 通过，8 个数据集摘要与样本数锁定一致 |
| `python scripts/checkpoint_drill.py --check` | 无 Redis 环境时正确报告未就绪（redis_ping=false, exit 1） |
| TypeScript `tsc --noEmit` | 通过 |
| Vite 生产构建 | 通过，28 个模块 |
| FastAPI 健康检查、OpenAPI 与缺失认证响应 | TestClient 验证通过 |
| PostgreSQL 容器启动 | 未完成，Docker 守护进程不可连接 |
| 在线迁移、种子导入、RLS 与并发售后事务 | 未运行，依赖数据库 |
| 模型、MinIO 上传、Neo4j 查询 | 未联调 |
| 在线评测维度（路由/无依据拒答/生成 SQL 与基准对照）与压测 | 未运行：需 API 服务、数据库与模型密钥；脚本已就绪（`eval_run.py --live`、`loadtest.py`） |
| Redis Stack 检查点跨进程恢复演练 | 未运行：Docker 不可用；脚本已就绪（`checkpoint_drill.py --run`） |
| GitHub Actions、容器镜像构建与备份恢复 | 配置已提供，未运行 |

前端最初构建时 shell 选中了 Node 16，Vite 无法运行；改用已安装的 Node 20.19.5 后构建成功，已在项目根目录及前端目录提供 `.nvmrc`。

售后与执行器测试使用 mock 验证服务契约；商品测试在 SQLite 上实际执行窗口排序及租户、店铺、价格、尺码筛选。它们不证明 PostgreSQL 的锁行为、并发唯一性、RLS 或执行计划。完整方案验收需继续执行实施进度中列出的集成场景。

## 代码优化回归

本轮新增 34 个回归用例，覆盖以下修复：

- 逐作用域检查事实聚合，拒绝聚合子查询、窗口函数和 UNION 分支夹带明细；重复别名返回校验失败，括号查询仍受行数上限约束。
- 商品在数据库中按 SPU 选最低价可售 SKU 后取前三，避免先截取 20 个 SKU 导致商品候选不足；换货库存与导购共用可售范围。
- 导购保留用户尺码和更严格的预算上限，跨 Agent 移交不放宽预算。
- 模型输出经严格字段校验，异常空值或类型返回受控失败；修复最多一次且保留原语义计划，生成尝试按实际次数计入预算。
- 执行器额外读取一行判断截断，取消查询后释放并发额度；租户参数转换不再修改 SQL 字符串字面量。

另将登录的 bcrypt 密码校验移至线程执行，避免阻塞异步事件循环；工具取消单独记录为 cancelled。本轮没有执行性能压测，不报告性能提升比例。

## 真流式与检查点回归

新增 14 个用例，覆盖：(1) 合成文案数值守门只放行上下文中的独立数字 token，越界立即终止，`Supervisor.stream()` 事件顺序为 progress → delta → answer，合成与摘要调用计入任务预算，预算耗尽或人工审批中断时不发起合成；(2) 摘要中间件的模型输出与注入消息不泄漏进回复；(3) 检查点后端 memory/auto 回退/redis 强制失败/redis 接线与释放（不依赖真实 Redis）；(4) 网关 `astream_chat` 逐块顺序与流式失败计入熔断。前端 `tsc --noEmit` 与 Vite 生产构建重新通过（28 个模块）。真实 Redis Stack 下的检查点跨进程恢复尚未演练，见实施进度下一阶段第 8 条。

## 语义计划与基准 SQL 回归

新增 51 个用例：语义计划校验（指标开放范围、维度口径授权、归集时间字段混算、UTC 半开区间与时区、过滤字段白名单、画像维度拒绝；非法计划不消耗模型调用预算且规范化计划进入提示词）；10 指标人工基准覆盖（每条通过 AST 校验、时间字段与口径定义一致、退款不与支付混算、客单价用 NULLIF 防除零、排名类含零销量）。

同时修复 AST 校验缺陷：sqlglot 将 AND/OR 建模为 `exp.Func` 子类，函数黑名单误判布尔连接词，会拦截全部带 WHERE 多条件的合法查询；已显式排除连接词，并补充合法多条件查询与连接词内危险函数两类回归（修复后 pg_sleep、dblink 等仍被拒绝）。

## 召回融合与槽位来源回归

新增 9 个用例：`merge_cards` 跨来源按商品去重（保留先出现候选，图谱不覆盖文本价格）、统一按（价格、SKU）排序与截断；导购结果装配中图谱补充候选不与文本候选重复、低价图谱商品排到文本候选之前；`slot_sources` 覆盖 payload/message/handoff 三类来源、跨来源最紧预算与移交值优先级；Supervisor 答案携带槽位来源记录（方案 §7/§13）。

既有用例同步适配卡片契约：测试桩数据补齐 `product_id`（去重与跳转依据，缺失标识的候选不进入卡片），并将图谱补充商品的断言更新为统一排序后的价格升序。

## OCR 核对与人工补录回归

新增 12 个用例（`tests/test_vision_verification.py`）：商品词表映射（标准词映射、未命中保留原值并标注 `unmapped_fields`、词表版本）；`verify_evidence` 精确核对分支（订单号去首尾空白全等、金额整数分相等、差一分判 mismatched、uncertain/missing/无订单事实/非整数分金额判 unverifiable、status 优先级）；人工补录（覆盖保留 OCR 原值与操作人、新增字段、非法字段与空值拒绝）。`create_draft` 完成识别后自动与订单事实核对并固化结论。接口新增 `POST /v1/uploads/{id}/verify` 与 `POST /v1/uploads/{id}/fields`（均要求图片已识别、写审计）。前端工作台新增图片凭证卡：上传 → 轮询识别状态 → 与所选订单精确核对 → 人工补录，商品图片可随消息发送识图；`tsc --noEmit` 与生产构建重新通过。识别仍不判断凭证真伪与责任归属。

## 知识导入审核与索引回归

新增 11 个用例（`tests/test_knowledge.py`）：切分定位逐段精确（块内容与原文 `char_start/char_end` 切片全等）、长段落句边界切分与超长硬切不丢字、`import_document` 版本冲突与审计、有效期校验、`publish_blockers` 分支（draft/索引未完成/空片段/片段未索引；keyword-only 显式降级允许发布）与 SQLite 真库发布/撤销。接口新增 `POST /v1/knowledge/documents` 与 `POST /v1/knowledge/{id}/review`；已有 publish 改用统一前置检查。Worker 新增 `knowledge_index` 任务（有密钥写回向量与索引版本，无密钥降级 keyword-only）。无依据拒答沿用售后确定性文案。

## 工单故障注入与队列退避回归

新增 14 个用例（`tests/test_worker_faults.py`）：`plan_after_create/plan_after_probe` 决策（created/timeout/unknown、无幂等平台走人工核对）、稳定请求号幂等创建与重复投递不再次调用外部、超时注入 → UNKNOWN + `next_retry_at` 退避、UNKNOWN 核对回 CREATED / 转 REVIEWING 人工分支 / 核对超时保持 UNKNOWN、重试超限终态失败。`src/worker.py` 重写：指数退避 + 抖动封顶、`locked_by/attempts/next_retry_at`、检索/识别/索引三类任务的终态外发同步。删除保留策略为任务与外发事件不自动清理（供审计）。当前适配器只创建本地演示单号，无真实外部网络写操作。

## 固定评测集与压测装置

`evals/cases/` 固定 8 个数据集：离线维度 61 条（语义计划 12、基准 SQL 口径 10、OCR 核对 16、状态机 14、知识管道 9），在线维度 23 条（意图路由 8、无依据拒答 5、生成 SQL 与人工基准对照 10）。`scripts/eval_run.py` 离线全通过并与 `evals/baseline.json` 摘要/样本数锁定一致（CI 已接入 `--check`）；在线维度需真实环境执行 `--live`。`scripts/loadtest.py` 压测脚本就绪（并发、P50/P95/P99、错误分类、JSON 报告），本轮未在真实服务上运行，不报告延迟数字。评测装置自检 12 个用例（`tests/test_eval_harness.py`）验证断言引擎、五个评测器非恒真与基线锁定负例。

## 检查点保留策略与跨进程演练装置

新增 `CHECKPOINT_TTL_MINUTES`（默认 43200 分钟，读取续期；0 表示不过期），`configure_checkpointer` 将其透传 `AsyncRedisSaver`。`scripts/checkpoint_drill.py` 提供真实 Redis Stack 演练：环境检查（PING/FT._LIST/强制 redis 装配）、阶段 A 子进程在 interrupt 审批门暂停落盘、阶段 B 新进程凭 thread_id `Command(resume=...)` 恢复并核对消息与摘要字段一致性、抽样检查点键 TTL。自检 10 个用例（`tests/test_checkpoint_drill.py`）覆盖 TTL 组装、透传与 auto 回退、内存后端下 interrupt/resume 图语义、核对项负例。真实跨进程恢复需 Docker/Redis Stack 就绪后执行 `--run`。


## 2026 年 10 月 18 日业务闭环补齐

| 检查 | 实际结果 |
| --- | --- |
| 全量 pytest | 269 passed；新增真实 SQLite + FastAPI 接口测试覆盖补料、取消、申诉路径、工单列表隔离、人工接管/回复/释放、历史分页及任务重放 |
| 商品检索回归 | 关键词实际查询、预算/租户过滤、Embedding 失败降级、非法向量拒绝、详情权限及库存复核通过；pgvector 在线距离查询未运行 |
| 多轮上下文 | 显式启用后继承服务端导购槽位；预算/品类变化及售后不继承场景通过；来源标记 conversation |
| 知识索引 | 不完整向量批次不能完成索引，文档作用域变更拒绝；预览及列表租户隔离通过 |
| 分析同步水位 | 执行器水位参数和返回契约测试通过；真实数据库快照/RLS 未联调 |
| 离线评测 | 61/61 通过，8 个数据集基线摘要一致；在线维度未运行 |
| 前端 | TypeScript 与 Vite 构建通过，29 个模块；未运行浏览器端到端测试 |
| 迁移与配置 | Alembic 0001 → 0002 离线 SQL、Python 编译、Compose 配置解析、bootstrap shell 语法、git diff --check 通过 |
| Docker | 已启动 Docker Desktop，服务端 29.6.1 可连接 |
| 基础设施启动 | 原 MinIO Docker Hub 镜像不可拉取；同版本 Quay 镜像返回 401。新增固定上游源码构建路径后，Go 基础镜像拉取遇到 auth.docker.io 网络超时；未进入在线迁移和初始化 |
| 模型配置 | 仅检查密钥是否存在，Chat 与视觉/Embedding 均未配置；不运行模型质量验收 |

新增 `scripts/demo_smoke.py` 提供真实 API 冒烟，尚未对在线服务执行。`docs/demo.md` 提供逐角色操作及故障演练步骤。上述离线检查不代表容器、S3 直传、Neo4j、Redis 恢复或真实模型效果已验收。

## 2026 年 10 月 18 日面试演示自查

自查范围按面试项目收敛：三 Agent 编排、商品约束与图谱、RAG 引用、多模态接口、受控 NL2SQL、自动售后及恢复链路。无需为演示继续扩展真实支付、完整物流、画像隐私分析或生产级队列。

本轮修复：

- 种子原来全部为签收订单，无法展示未发货自动退款。新种子保留 DEMO-0000 的签收及目标尺码缺货场景，将 DEMO-0002/0003 设为未发货已支付。
- 种子日期原来固定于 2026 年 10 月，未来演示会超期。新建数据使用初始化时点构建相对日期，ID 与随机商品值仍固定；已有数据不被覆盖，不承诺旧订单永不过期。
- Worker 未捕获异常原来会终止进程。现在用 SAVEPOINT 回滚单次业务处理，保留尝试次数并退避；连接故障后等待重连。售后查询可见任务异常提示，重试成功清除旧错误。SQLite 真事务测试验证适配器异常后的业务回滚、第二次建单与 outbox 一致性；未证明 PostgreSQL 并发锁行为。
- 在线冒烟原来只验证草稿取消，未覆盖自动审核。新增 `--automatic-aftersale` 显式核验确认、幂等重放、Worker 自动审核及沙箱创建；装置对 NEED_MORE_INFO 和已占用订单明确失败，不误报通过。该在线选项本轮未对服务执行。

| 实际执行 | 结果 |
| --- | --- |
| 全量 pytest | 308 passed |
| 离线评测与数据集锁定 | 61/61，通过；在线维度未运行 |
| 前端 TypeScript 与 Vite 构建 | 通过 |
| Python 编译与 pip check | 通过，无依赖冲突 |
| Alembic 0001 → 0003 离线 SQL | 生成通过，未执行真实数据库迁移 |
| Compose 配置、bootstrap shell 语法、diff 空白检查 | 通过 |
| Docker 与 API | 当前无 Docker socket，API 8090 不可连接，未执行在线冒烟 |
| Provider 配置存在性 | Chat Key 已配置，视觉/Embedding Key 未配置；未打印或读取给用户任何密钥值，未执行在线模型验收 |

演示建议保留四条主线：预算和尺码约束导购及图谱关系；缺货换码到同品牌替代的跨 Agent 协作；用户确认到规则自动审核、沙箱建单；运营自然语言到受控 SQL 与数据表。多模态作为配置视觉 Provider 后的增强演示。换货自动执行、真实退款完成、完整画像分析均不作为已经交付的能力。

结论：特色功能和离线逻辑足以支撑面试项目范围，但当前证据不足以声称完整环境链路已跑通。面试前须启动基础设施、迁移、初始化、配置视觉 Provider 并实际运行冒烟和在线评测，保存工具轨迹、SQL 结果和截图。不可用离线测试通过率冒充模型准确率。

## 2026 年 10 月 18 日实际启动与 Embedding 联调

- 已读取独立 `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL`，兼容 `EMBEDDING_DIMENSION`；实际模型返回 1024 维向量。未记录密钥值。
- Docker Desktop 已启动。完整版 bootstrap 在 Docker Hub 鉴权端点网络超时，未完成完整镜像构建。
- 改用本机缓存镜像和 `docker-compose.local.yml`：PostgreSQL 16 + pgvector、Redis 7、MinIO；独立卷。API、Worker、Vite 在宿主机启动，检查点使用 memory。
- PostgreSQL 真实迁移 0001 → 0003、固定种子、分析投影同步和私有桶初始化均已执行成功。商品向量索引完成 60 项。
- `scripts.demo_smoke --automatic-aftersale` 实际通过：模型导购、预算/库存、草稿取消持久化、消息保存、另一买家 404、提交幂等重放、自动规则审核与沙箱建单。生成的工单可在买家售后列表查看。
- 在线模型批量工具请求曾超过剩余额度，已修复为汇总已核验结果；未授权工具仍拒绝，回归通过。
- 全量测试 310 passed；前端根页面和 API 健康检查实际返回成功。
- Neo4j 尚未启动，视觉 Key 未配置，Redis Stack 跨进程恢复未验收；没有运行完整在线评测集或性能压测。
