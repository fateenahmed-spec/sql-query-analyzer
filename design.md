# AI-Assisted SQL Query Analyzer — Data Contracts

This file is the source of truth for parsed plans, warnings, animation, and demo data.

## PlanNode

Each node has `{ id, operator, details, depth, estimated_rows, estimated_cost, actual_rows, loops, rows_total, time_first_ms, time_last_ms, time_total_ms, self_time_ms, self_time_pct, never_executed, warnings, children }`. IDs are pre-order (`node_1`, `node_2`, ...). `actual_rows`, `time_first_ms`, and `time_last_ms` are per loop. `rows_total = actual_rows * loops`; `time_total_ms = time_last_ms * loops`; `self_time_ms = max(0, time_total_ms - sum(child.time_total_ms))`.

Never-executed nodes have `loops=0`, `rows_total=0`, all times zero, and `never_executed=true`.

## Warning

`{ code, severity, node_id, title, message, suggestion }`; severity is `high`, `medium`, or `low`, and suggestion may be null.

## AnimationStep

`{ step, node_id, parent_id, action, rows_out, loops }`. Steps are post-order: children before parents, every node exactly once, with the root last. Never-executed nodes remain present with `rows_out=0`. Action is `scan`, `filter`, `join`, `aggregate`, `sort`, `limit`, or `other`. `rows_out = rows_total`.

## Visual language and demo data

Scans use yellow dots; filters use pink dots; rows passed to the next node use green dots. Demo data lives under `demo/`. The loader prefers real captures in `demo/plans/` and falls back to hand-written plans in `demo/plans_fallback/`.
