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

  function operationDetails(node) {
    const details = typeof node.details === "string" ? node.details.trim() : "";
    if (details) return details;

    if (operatorLabel(node).toLowerCase().includes("join") && Array.isArray(node.children)) {
      const inputDetails = node.children
        .filter((child) => child && typeof child.details === "string" && child.details.trim())
        .map((child) => `${operatorLabel(child)}: ${child.details.trim()}`);
      if (inputDetails.length) return `Input operation details: ${inputDetails.join(" · ")}`;
    }

    return "No condition or additional operation details were reported for this node.";
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

  function buildNodeInspector(node) {
    return {
      operation: operatorLabel(node),
      details: operationDetails(node),
      metrics: [
        {
          label: "Actual Rows",
          value: validMetric(node.actual_rows) ? numberLabel(node.actual_rows) + " rows" : "Unavailable",
          help: "Average rows produced per execution loop, as reported by MySQL.",
        },
        {
          label: "Loops",
          value: Number.isInteger(node.loops) && validMetric(node.loops)
            ? numberLabel(node.loops)
            : "Unavailable",
        },
        {
          label: "Execution Time",
          value: validMetric(node.time_total_ms) ? timeLabel(node.time_total_ms) : "Unavailable",
          help: "Total elapsed time across all loops for this operation, including its child operations.",
        },
      ],
    };
  }

  return { buildGraphLabel, buildNodeInspector };
});
