const assert = require("node:assert/strict");
const test = require("node:test");

const { buildGraphLabel, buildNodeInspector } = require("../frontend/plan_labels.js");

test("omits unavailable, invalid, and unexecuted metrics", () => {
  const label = buildGraphLabel({
    operator: "Table scan",
    actual_rows: null,
    rows_total: null,
    loops: null,
    self_time_ms: null,
    estimated_rows: null,
    estimated_cost: null,
  });

  assert.equal(label, "Table scan");
  assert.doesNotMatch(label, /unavailable|undefined|null|rows\/loop|cost|time/i);
  assert.equal(buildGraphLabel({
    operator: "Filter",
    actual_rows: "8",
    rows_total: Infinity,
    loops: -1,
    self_time_ms: "12",
  }), "Filter");
  assert.equal(buildGraphLabel({
    operator: "Sort",
    actual_rows: 0,
    rows_total: 0,
    loops: 0,
    self_time_ms: 0,
    never_executed: true,
  }), "Sort");
});

test("shows actual output rows and relevant self time when available", () => {
  assert.equal(buildGraphLabel({
    operator: "Table scan",
    actual_rows: 8,
    rows_total: 8,
    loops: 1,
    self_time_ms: 4.25,
    time_total_ms: 54.2,
  }), "Table scan\nActual rows: 8\nSelf time: 4.25 ms");
});

test("aggregates repeated-loop rows without presenting the average as a total", () => {
  assert.equal(buildGraphLabel({
    operator: "Index lookup",
    actual_rows: 8,
    rows_total: 16,
    loops: 2,
    self_time_ms: 1.5,
  }), "Index lookup\nActual rows: ~16\nSelf time: 1.5 ms");
});

test("labels per-loop output when no reliable loop total is available", () => {
  assert.equal(buildGraphLabel({
    operator: "Nested loop inner join",
    actual_rows: 2,
    rows_total: null,
    loops: 3,
    self_time_ms: 0,
  }), "Nested loop inner join\nActual rows per loop: 2");
  assert.equal(buildGraphLabel({
    operator: "Filter",
    actual_rows: 2,
    loops: null,
  }), "Filter\nActual rows per loop: 2");
});

test("keeps concise operator names for different plan operations", () => {
  for (const operator of ["Filter", "Hash join", "Aggregate", "Sort", "Limit", "Materialize"]) {
    assert.equal(buildGraphLabel({ operator }), operator);
  }
});

test("explains complete row, estimate, loop, and timing metrics without mixing them", () => {
  const inspector = buildNodeInspector({
    operator: "Nested loop inner join",
    details: "(c.id=o.customer_id)",
    actual_rows: 8,
    rows_total: 16,
    loops: 2,
    estimated_rows: 6,
    estimated_cost: 42,
    time_first_ms: 0.4,
    time_last_ms: 2.5,
    time_total_ms: 5,
    self_time_ms: 3,
    self_time_pct: 30,
  }, [], 10);

  assert.match(inspector.explanation, /join inputs/i);
  assert.equal(inspector.details, "(c.id=o.customer_id)");
  assert.deepEqual(inspector.rowsMetrics.map(({ label, value }) => [label, value]), [
    ["Rows returned per run (average)", "8 rows"],
    ["Number of runs", "2"],
    ["Approximate total across runs", "About 16 rows"],
  ]);
  assert.deepEqual(inspector.timeMetrics.map(({ label, value }) => [label, value]), [
    ["Time to first row (average per run)", "0.4 ms"],
    ["Time to all rows (average per run)", "2.5 ms"],
    ["Total time in this operation (excluding child steps)", "3 ms"],
    ["Total time for this operation and child steps", "5 ms"],
    ["Share of measured plan time", "30%"],
  ]);
  assert.deepEqual(inspector.estimateMetrics.map(({ label, value }) => [label, value]), [
    ["Predicted rows per run", "6 rows"],
    ["Estimated work (cost)", "42"],
  ]);
  assert.match(inspector.timeMetrics[3].help, /do not add/i);
});

test("hides absent and invalid estimate metrics instead of showing unavailable values", () => {
  const inspector = buildNodeInspector({
    operator: "Table scan",
    details: "on employees",
    estimated_rows: null,
    estimated_cost: "unknown",
    actual_rows: null,
    loops: null,
    time_first_ms: NaN,
    time_last_ms: null,
    time_total_ms: null,
    self_time_ms: 0,
    self_time_pct: 0,
  });

  assert.match(inspector.explanation, /without using an index/i);
  assert.match(inspector.explanation, /reasonable/i);
  assert.deepEqual(inspector.estimateMetrics, []);
  assert.deepEqual(inspector.rowsMetrics, []);
  assert.deepEqual(inspector.timeMetrics, []);
  assert.doesNotMatch(JSON.stringify(inspector), /unavailable|undefined|null/i);
});

test("keeps zero-row results distinct from never-executed operations", () => {
  const zeroRows = buildNodeInspector({
    operator: "Filter",
    actual_rows: 0,
    rows_total: 0,
    loops: 1,
    time_first_ms: 0,
    time_last_ms: 0,
    time_total_ms: 0,
    self_time_ms: 0,
    estimated_rows: 3,
  });
  assert.equal(zeroRows.status, "This operation ran and returned no rows.");
  assert.deepEqual(zeroRows.rowsMetrics.slice(0, 2).map((metric) => metric.value), ["0 rows", "1"]);

  const skipped = buildNodeInspector({
    operator: "Index lookup",
    never_executed: true,
    actual_rows: 0,
    rows_total: 0,
    loops: 0,
    time_first_ms: 0,
    time_last_ms: 0,
    time_total_ms: 0,
    self_time_ms: 0,
    estimated_rows: 1,
    estimated_cost: 2,
  });
  assert.equal(skipped.status, "MySQL did not execute this operation for the query.");
  assert.deepEqual(skipped.rowsMetrics, []);
  assert.deepEqual(skipped.timeMetrics, []);
  assert.equal(skipped.estimateMetrics.length, 2);
});

test("uses operation-specific plain-language explanations", () => {
  const explanations = [
    ["Aggregate using temporary table", /temporary storage.*common for group by.*not by itself mean/i],
    ["Inner hash join", /hash-based join/i],
    ["Filter", /checked a condition/i],
    ["Sort", /ordered rows/i],
    ["Limit", /limited how many rows/i],
    ["Group aggregate", /grouped rows|aggregate values/i],
    ["Materialize with deduplication", /stored intermediate results.*removed duplicates/i],
    ["Single-row index lookup", /used an index/i],
  ];

  for (const [operator, expected] of explanations) {
    assert.match(buildNodeInspector({ operator }).explanation, expected, operator);
  }
});

test("keeps warning explanations, evidence, and suggestions in the inspector model", () => {
  const inspector = buildNodeInspector({ operator: "Sort" }, [{
    code: "FULL_SCAN",
    severity: "low",
    node_id: "node_2",
    title: "MySQL scanned many rows",
    message: "A scan can be reasonable when many rows are needed.",
    evidence: "Actual rows: 20000.",
    suggestion: "Review the filter if the query is slow.",
  }]);

  assert.deepEqual(inspector.warnings[0], {
    severity: "low",
    title: "MySQL scanned many rows",
    message: "A scan can be reasonable when many rows are needed.",
    evidence: "Actual rows: 20000.",
    suggestion: "Review the filter if the query is slow.",
    code: "FULL_SCAN",
    nodeId: "node_2",
  });
});
