"""SQL AST 校验器（方案 §6）。

使用 PostgreSQL 方言 AST 解析，限制单语句，递归审查 CTE、子查询、
集合运算、表、字段、函数和所有写入节点；不能只检查首个 SELECT。
解析成功不等于授权或执行安全，本校验是执行前的硬门槛。
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp, parse
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from src.modules.analytics.metrics import ALLOWED_COLUMNS, ALLOWED_TABLES

# 危险函数黑名单（禁止执行 / 探测）
BANNED_FUNCTIONS = {
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "lo_import",
    "lo_export",
    "dblink",
    "dblink_exec",
    "current_setting",
    "set_config",
    "pg_write_file",
}

# 顶层允许的语句类型（其余一律拒绝）
_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)
FACT_TABLES = frozenset({"fact_order_items", "fact_refunds"})

# 任何位置都禁止出现的写/DDL/DCL 节点
_FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Merge,
    exp.Command,
    exp.Grant,
    exp.Copy,
    exp.Set,
    exp.Use,
)

ALLOWED_FUNCTIONS = {
    "sum", "count", "avg", "min", "max", "coalesce", "nullif", "round",
    "cast", "extract", "date_trunc", "timestamp_trunc", "date", "abs",
    "row_number", "rank", "dense_rank", "lower", "upper", "greatest", "least",
    "current_date", "current_timestamp", "case", "if", "to_char", "timezone",
}


@dataclass
class ValidationResult:
    """校验结果。"""

    ok: bool
    reason: str = ""
    normalized_sql: str = ""
    error_kind: str = ""  # syntax / unknown_column / unauthorized / dangerous / unsupported


def _collect_schema() -> dict[str, dict[str, str]]:
    """构造 sqlglot qualify 所需的 schema 结构。"""
    schema: dict[str, dict[str, str]] = {}
    for table, columns in ALLOWED_COLUMNS.items():
        schema[table] = {col: "TEXT" for col in columns}
    return schema


def validate_sql(sql: str, *, max_rows: int = 1000) -> ValidationResult:
    """校验并规范化 SQL。

    返回 normalized_sql（注入 LIMIT 后的 PostgreSQL 方言 SQL）。
    """
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
        return ValidationResult(ok=False, reason="结果上限必须是正整数", error_kind="unsupported")
    sql = (sql or "").strip()
    if not sql:
        return ValidationResult(ok=False, reason="SQL 为空", error_kind="syntax")

    # 1. 解析与单语句限制
    try:
        statements = parse(sql, dialect="postgres")
    except sqlglot.errors.ParseError as exc:
        return ValidationResult(
            ok=False, reason=f"SQL 语法解析失败：{exc}", error_kind="syntax"
        )
    statements = [s for s in statements if s is not None]
    if len(statements) == 0:
        return ValidationResult(ok=False, reason="未解析到语句", error_kind="syntax")
    if len(statements) > 1:
        return ValidationResult(
            ok=False, reason="仅允许单条语句", error_kind="unauthorized"
        )

    ast = statements[0]
    # 括号不应绕过根查询类型和结果数量检查。
    while isinstance(ast, exp.Subquery):
        ast = ast.this

    # 2. 顶层语句类型
    if not isinstance(ast, _ALLOWED_ROOTS):
        return ValidationResult(
            ok=False,
            reason=f"仅允许 SELECT 查询，收到：{type(ast).__name__}",
            error_kind="unauthorized",
        )

    # 3. 递归检查：任何位置的写/DDL/DCL 节点（CTE、子查询、集合运算全覆盖）
    for node in ast.walk():
        if isinstance(node, exp.Placeholder):
            return ValidationResult(ok=False, reason="模型查询不得包含自定义参数", error_kind="unauthorized")
        if isinstance(node, _FORBIDDEN_NODES):
            return ValidationResult(
                ok=False,
                reason=f"检测到禁止的语句节点：{type(node).__name__}",
                error_kind="unauthorized",
            )

    # 4. SELECT ... INTO 禁止
    if any(s.args.get("into") or s.args.get("locks") for s in ast.find_all(exp.Select)):
        return ValidationResult(ok=False, reason="禁止 SELECT INTO 与行锁查询", error_kind="unauthorized")

    # 5. 表白名单（递归所有 Table 节点，含 CTE 定义与 JOIN）
    physical_tables = []
    try:
        for scope in traverse_scope(ast):
            for _, source in scope.selected_sources.values():
                if isinstance(source, exp.Table):
                    physical_tables.append(source)
    except sqlglot.errors.OptimizeError:
        return ValidationResult(ok=False, reason="表别名重复或查询作用域有歧义", error_kind="syntax")
    for table in physical_tables:
        name = table.name
        db = table.db or ""
        if table.catalog or db or not isinstance(table.this, exp.Identifier):
            return ValidationResult(
                ok=False, reason=f"禁止访问系统表：{db}.{name}", error_kind="unauthorized"
            )
        if name not in ALLOWED_TABLES:
            return ValidationResult(
                ok=False,
                reason=f"表不在授权数据集内：{name}",
                error_kind="unauthorized",
            )

    # 6. 函数黑名单（含匿名函数与命名函数）。
    # AND/OR 等布尔连接词在 sqlglot 中是 Func 子类，但属于结构谓词而非函数调用；
    # 若不排除，所有带 WHERE 多条件的合法查询都会被误判为危险函数。
    for func in ast.find_all(exp.Func):
        if isinstance(func, exp.Connector):
            continue
        func_name = ""
        if isinstance(func, exp.Anonymous):
            func_name = (func.name or "").lower()
        else:
            func_name = func.sql_name().lower()
        if func_name in BANNED_FUNCTIONS or func_name not in ALLOWED_FUNCTIONS:
            return ValidationResult(
                ok=False, reason=f"禁止调用危险函数：{func_name}", error_kind="dangerous"
            )

    # 7. 列白名单：无法确定字段归属时拒绝执行。
    try:
        qualified = qualify(
            ast.copy(),
            schema=_collect_schema(),
            validate_qualify_columns=True,
            quote_identifiers=False,
            identify=False,
            expand_stars=True,
        )
        for column in qualified.find_all(exp.Column):
            table_name = column.table
            col_name = column.name
            if not table_name:
                continue
            if table_name in ALLOWED_COLUMNS and col_name not in ALLOWED_COLUMNS[table_name]:
                return ValidationResult(
                    ok=False,
                    reason=f"列不在授权数据集内：{table_name}.{col_name}",
                    error_kind="unknown_column",
                )
        ast = qualified
    except Exception:
        return ValidationResult(ok=False, reason="字段不存在或字段引用有歧义", error_kind="unknown_column")

    if any(c.name in {"region", "age_group"} for c in ast.find_all(exp.Column)):
        return ValidationResult(ok=False, reason="画像小群组保护尚未启用，该维度暂不开放", error_kind="unsupported")
    for scope in traverse_scope(ast):
        reads_facts = any(isinstance(source, exp.Table) and source.name in FACT_TABLES
                          for _, source in scope.selected_sources.values())
        if not reads_facts:
            continue
        # 聚合必须发生在直接读取事实的 SELECT 中。其他子查询的 SUM、窗口
        # 聚合不会降低当前查询的明细粒度，不能据此放行。
        select = scope.expression
        has_aggregate = any(
            aggregate.find_ancestor(exp.Select) is select
            and aggregate.find_ancestor(exp.Window) is None
            for aggregate in select.find_all(exp.AggFunc)
        )
        if not has_aggregate:
            return ValidationResult(ok=False, reason="每个读取交易事实的查询都必须先聚合", error_kind="unauthorized")
    for column in ast.find_all(exp.Column):
        if column.name != "order_no":
            continue
        # 只允许 COUNT(DISTINCT order_no)，避免 DISTINCT 本身被当成脱敏。
        parent = column.parent
        if not (isinstance(parent, exp.Distinct) and isinstance(parent.parent, exp.Count)):
            return ValidationResult(ok=False, reason="订单标识仅允许用于去重计数", error_kind="unauthorized")

    # 8. 强制注入 LIMIT（防结果集过大）
    if isinstance(ast, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        if ast.args.get("limit") is None:
            ast = ast.limit(max_rows)
        else:
            value = ast.args["limit"].expression
            if not isinstance(value, exp.Literal) or not value.is_int or int(value.this) < 0:
                return ValidationResult(ok=False, reason="LIMIT 必须是非负整数", error_kind="unsupported")
            ast = ast.limit(min(int(value.this), max_rows))

    return ValidationResult(
        ok=True,
        reason="",
        normalized_sql=ast.sql(dialect="postgres"),
    )
