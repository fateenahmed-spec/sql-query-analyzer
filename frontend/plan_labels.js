(function exposePlanLabels(root, factory) {
  const planLabels = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = planLabels;
  } else {
    root.QueryLensPlanLabels = planLabels;
  }
})(typeof globalThis === "undefined" ? this : globalThis, function createPlanLabels() {
  function validMetric(value) {
    return typeof value === "number" && Number.isFinite(value) && value >= 0;
  }

  function numberLabel(value) {
    return value.toLocaleString(undefined, { maximumFractionDigits: 6 });
  }

  function timeLabel(value) {
    return `${Number(value.toPrecision(4)).toLocaleString()} ms`;
  }

  function operatorLabel(node) {
    return typeof node.operator === "string" && node.operator.trim()
      ? node.operator.trim()
      : "Operator";
  }

  function buildGraphLabel(node) {
    const operator = operatorLabel(node);
    const lines = [operator];
    const actualRows = validMetric(node.actual_rows) ? node.actual_rows : null;
    const loops = Number.isInteger(node.loops) && validMetric(node.loops) ? node.loops : null;
    const totalRows = validMetric(node.rows_total) ? node.rows_total : null;

    if (!node.never_executed && actualRows !== null && loops !== 0) {
      if (loops === 1) {
        lines.push(`Actual rows: ${numberLabel(actualRows)}`);
      } else if (loops !== null && totalRows !== null) {
        lines.push(`Actual rows: ~${numberLabel(totalRows)}`);
      } else {
        lines.push(`Actual rows per loop: ${numberLabel(actualRows)}`);
      }
    }

    if (!node.never_executed && validMetric(node.self_time_ms) && node.self_time_ms > 0) {
      lines.push(`Self time: ${timeLabel(node.self_time_ms)}`);
    }

    return lines.join("\n");
  }

  function operatorExplanation(node) {
    const operator = operatorLabel(node);
    const normalized = operator.toLowerCase();
    const details = typeof node.details === "string" ? node.details.trim() : "";
    const table = details.replace(/^on\s+/i, "").split(/\s+using\s+/i)[0];

    if (normalized.includes("aggregate using temporary table")) {
      return "MySQL grouped rows and calculated results using internal temporary storage. This is common for GROUP BY and does not by itself mean the query is slow.";
    }
    if (normalized.includes("table scan")) {
      return `MySQL read rows${table ? ` from ${table}` : ""} without using an index to find them. This can be reasonable for a small table or when many rows are needed.`;
    }
    if (normalized.includes("covering index")) {
      return `MySQL read the needed values from an index${table ? ` for ${table}` : ""}. A covering index can answer this step without looking up the table rows.`;
    }
    if (normalized.includes("index")) {
      return `MySQL used an index${table ? ` on ${table}` : ""} to find or read rows.`;
    }
    if (normalized.includes("hash join")) {
      return "MySQL matched rows from two inputs using a hash-based join. This is a join strategy, not a problem by itself.";
    }
    if (normalized.includes("nested loop") || normalized.includes("join")) {
      return "MySQL matched rows from the join inputs by checking one input against the other. This is a normal join strategy.";
    }
    if (normalized === "filter") {
      return "MySQL checked a condition and passed matching rows to the next step.";
    }
    if (normalized === "sort") {
      return "MySQL ordered rows for this step. Sorting may be needed to satisfy ORDER BY or a later operation.";
    }
    if (normalized === "limit") {
      return "MySQL limited how many rows this step passed to the next operation.";
    }
    if (normalized.includes("aggregate") || normalized.includes("group aggregate")) {
      return "MySQL grouped rows or calculated aggregate values from the input.";
    }
    if (normalized.includes("materialize")) {
      return normalized.includes("deduplication")
        ? "MySQL temporarily stored intermediate results and removed duplicates while preparing them for another step."
        : "MySQL temporarily stored intermediate results so another step could use them.";
    }
    if (normalized === "hash") {
      return "MySQL built a hash structure for another operation, commonly a hash join.";
    }
    if (normalized === "zero rows") {
      return "MySQL identified a step that produces no rows for this plan.";
    }
    if (normalized === "rows fetched before execution") {
      return "MySQL obtained these rows before continuing with the rest of the plan.";
    }
    return `MySQL used the ${operator} operation to produce the query results.`;
  }

  function cleanWarnings(warnings) {
    return (Array.isArray(warnings) ? warnings : []).map((warning) => {
      const severity = ["high", "medium", "low", "info"].includes(String(warning.severity || "").toLowerCase())
        ? String(warning.severity).toLowerCase()
        : "info";
      return {
        severity,
        title: typeof warning.title === "string" && warning.title.trim()
          ? warning.title.trim()
          : warning.code || warning.type || "Plan warning",
        message: typeof warning.message === "string" ? warning.message.trim() : "",
        evidence: typeof warning.evidence === "string" ? warning.evidence.trim() : "",
        suggestion: typeof warning.suggestion === "string" ? warning.suggestion.trim() : "",
        code: typeof warning.code === "string" ? warning.code : "",
        nodeId: typeof warning.node_id === "string" ? warning.node_id : "",
      };
    });
  }

  function buildNodeInspector(node, warnings = [], rootTime = null) {
    const operator = operatorLabel(node);
    const details = typeof node.details === "string" ? node.details.trim() : "";
    const neverExecuted = node.never_executed === true;
    const loops = !neverExecuted && Number.isInteger(node.loops) && validMetric(node.loops)
      ? node.loops
      : null;
    const actualRows = !neverExecuted && validMetric(node.actual_rows) && loops !== 0
      ? node.actual_rows
      : null;
    const actualMetrics = [];
    const timeMetrics = [];
    const estimateMetrics = [];

    if (actualRows !== null) {
      actualMetrics.push({
        label: "Rows returned per run (average)",
        value: `${numberLabel(actualRows)} rows`,
        help: "This is MySQL's measured output for one run of this operation. With multiple runs, it is an average.",
      });
    }
    if (loops !== null && loops > 0) {
      actualMetrics.push({
        label: "Number of runs",
        value: numberLabel(loops),
        help: "How many times MySQL executed this operation.",
      });
    }

    if (actualRows !== null && loops !== null && loops > 1 && validMetric(node.rows_total)) {
      const calculatedRows = actualRows * loops;
      const tolerance = Math.max(1e-9, Math.abs(calculatedRows) * 1e-9);
      if (Math.abs(calculatedRows - node.rows_total) <= tolerance) {
        actualMetrics.push({
          label: "Approximate total across runs",
          value: `About ${numberLabel(node.rows_total)} rows`,
          help: "Calculated from MySQL's average rows per run multiplied by the run count, so treat this total as approximate.",
        });
      }
    }

    if (!neverExecuted && loops !== 0) {
      if (validMetric(node.time_first_ms)) {
        timeMetrics.push({
          label: loops > 1 ? "Time to first row (average per run)" : "Time to first row",
          value: timeLabel(node.time_first_ms),
          help: "Elapsed time until this operation produced its first row; this is not its completion time.",
        });
      }
      if (validMetric(node.time_last_ms)) {
        timeMetrics.push({
          label: loops > 1 ? "Time to all rows (average per run)" : "Time to all rows",
          value: timeLabel(node.time_last_ms),
          help: "Elapsed time until this operation finished producing rows for a run.",
        });
      }
      if (validMetric(node.time_total_ms) && validMetric(node.self_time_ms)) {
        timeMetrics.push({
          label: "Total time in this operation (excluding child steps)",
          value: timeLabel(node.self_time_ms),
          help: "Measured time attributed to this operation across all runs after time in its child steps is excluded.",
        });
      }
      if (validMetric(node.time_total_ms)) {
        timeMetrics.push({
          label: "Total time for this operation and child steps",
          value: timeLabel(node.time_total_ms),
          help: "Total across runs, including child work. These timings overlap; do not add parent and child totals together.",
        });
      }
      if (validMetric(rootTime) && rootTime > 0 && validMetric(node.time_total_ms) && validMetric(node.self_time_pct)) {
        timeMetrics.push({
          label: "Share of measured plan time",
          value: `${numberLabel(node.self_time_pct)}%`,
          help: "Percentage of the plan's measured time attributed to this operation's own work.",
        });
      }
    }

    if (validMetric(node.estimated_rows)) {
      estimateMetrics.push({
        label: "Predicted rows per run",
        value: `${numberLabel(node.estimated_rows)} rows`,
        help: "MySQL's prediction before execution. This is an estimate, not a measured row count.",
      });
    }
    if (validMetric(node.estimated_cost)) {
      estimateMetrics.push({
        label: "Estimated work (cost)",
        value: numberLabel(node.estimated_cost),
        help: "MySQL's optimizer work estimate. Cost is not a time measurement.",
      });
    }

    let status;
    if (neverExecuted) {
      status = "MySQL did not execute this operation for the query.";
    } else if (loops === 0) {
      status = "MySQL reported that this operation ran zero times.";
    } else if (actualRows === 0 && loops === 1) {
      status = "This operation ran and returned no rows.";
    } else if (actualRows === 0 && loops !== null && loops > 1) {
      status = "MySQL reported an average of zero output rows per run.";
    } else if (actualRows === 0) {
      status = "MySQL reported zero output rows for this operation.";
    } else if (actualRows !== null) {
      status = "This operation ran and produced rows.";
    } else if (timeMetrics.length > 0) {
      status = "MySQL recorded timing measurements for this operation.";
    } else {
      status = "The plan did not include actual execution measurements for this operation.";
    }

    return {
      operator,
      explanation: operatorExplanation(node),
      details,
      rowsSummary: actualMetrics.length
        ? "These are measurements from query execution. Row counts are shown per run; any calculated total is marked approximate."
        : "No actual row count was included for this operation.",
      rowsMetrics: actualMetrics,
      timeSummary: timeMetrics.length
        ? "Per-run timings describe one loop. Total inclusive time includes child work and can overlap with child timings."
        : "No timing measurements were included for this operation.",
      timeMetrics,
      estimateSummary: "These are MySQL predictions made while planning the query, not actual results or elapsed time.",
      estimateMetrics,
      status,
      warnings: cleanWarnings(warnings),
    };
  }

  return { buildGraphLabel, buildNodeInspector };
});
