"""预算核算的数据表名与状态常量。

单独成模块是为了让 app.store 在初始化阶段即可安全导入表名，
而不触发对 app.services.budget（它依赖 store 单例）的循环导入。
"""
from __future__ import annotations

# 原始数据表（种子与导入写入）
T_BUDGET_ITEMS = "budget_items"
T_VOUCHERS = "expense_vouchers"
T_ALLOCATIONS = "expense_allocations"
T_REVERSALS = "reversal_entries"
T_EXCEPTIONS = "budget_exceptions"
T_BATCHES = "budget_import_batches"
# 核算产出表（引擎写入，不可变）
T_RUNS = "budget_runs"
T_BASES = "budget_bases"
T_EXECUTION = "budget_execution"
T_ATTACHMENTS = "budget_attachments"

ALL_TABLES = [
    T_BUDGET_ITEMS, T_VOUCHERS, T_ALLOCATIONS, T_REVERSALS, T_EXCEPTIONS,
    T_BATCHES, T_RUNS, T_BASES, T_EXECUTION, T_ATTACHMENTS,
]

PERIOD_GRANULARITY = "quarter"
VOUCHER_VALID = "有效"
VOUCHER_INVALID = "无效"
REVERSAL_DONE = "已完成"
REVERSAL_PENDING = "未完成"
