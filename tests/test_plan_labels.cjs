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

test("shows operation details and only essential execution metrics", () => {
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
  });

  assert.equal(inspector.operation, "Nested loop inner join");
  assert.equal(inspector.details, "(c.id=o.customer_id)");
  assert.deepEqual(inspector.metrics.map(({ label, value }) => [label, value]), [
    ["Actual Rows", "8 rows"],
    ["Loops", "2"],
    ["Execution Time", "5 ms"],
  ]);
  assert.doesNotMatch(JSON.stringify(inspector), /estimated|self time|first row|all rows|approximate|node_\d+/i);
  assert.match(inspector.metrics[0].help, /average rows produced per execution loop/i);
  assert.match(inspector.metrics[2].help, /across all loops.*including its child operations/i);
});

test("handles missing and invalid metrics gracefully", () => {
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

  assert.deepEqual(inspector.metrics.map(({ label, value }) => [label, value]), [
    ["Actual Rows", "Unavailable"],
    ["Loops", "Unavailable"],
    ["Execution Time", "Unavailable"],
  ]);
  assert.equal(inspector.details, "on employees");
});

test("shows zero metrics for nodes that returned no rows or were not executed", () => {
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
  assert.deepEqual(zeroRows.metrics.map(({ label, value }) => [label, value]), [
    ["Actual Rows", "0 rows"],
    ["Loops", "1"],
    ["Execution Time", "0 ms"],
  ]);

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
  assert.deepEqual(skipped.metrics.map(({ label, value }) => [label, value]), [
    ["Actual Rows", "0 rows"],
    ["Loops", "0"],
    ["Execution Time", "0 ms"],
  ]);
});

test("shows supported node types in the Operation field", () => {
  for (const operator of [
    "Table scan",
    "Filter",
    "Nested loop inner join",
    "Sort",
    "Aggregate using temporary table",
    "Window aggregate",
    "Limit",
  ]) {
    assert.equal(buildNodeInspector({ operator }).operation, operator);
  }
});

test("builds fresh values when a different node is selected", () => {
  const scan = buildNodeInspector({ operator: "Table scan", details: "on orders", actual_rows: 500, loops: 1, time_total_ms: 12 });
  const filter = buildNodeInspector({ operator: "Filter", details: "(amount > 500)", actual_rows: 5, loops: 1, time_total_ms: 2 });

  assert.equal(scan.operation, "Table scan");
  assert.equal(scan.details, "on orders");
  assert.deepEqual(scan.metrics.map(({ value }) => value), ["500 rows", "1", "12 ms"]);
  assert.equal(filter.operation, "Filter");
  assert.equal(filter.details, "(amount > 500)");
  assert.deepEqual(filter.metrics.map(({ value }) => value), ["5 rows", "1", "2 ms"]);
  assert.notEqual(scan.metrics, filter.metrics);
});

test("falls back to a generic operation label when the operator is missing", () => {
  const inspector = buildNodeInspector({ actual_rows: 3, loops: 1, time_total_ms: 0.5 });
  assert.equal(inspector.operation, "Operator");
  assert.equal(inspector.details, "No condition or additional operation details were reported for this node.");
});

test("shows parsed child lookup details for a nested-loop join without its own condition", () => {
  const inspector = buildNodeInspector({
    operator: "Nested loop inner join",
    details: "",
    children: [
      { operator: "Filter", details: "(o.amount > 900.00)" },
      { operator: "Single-row index lookup", details: "on c using PRIMARY (id=o.customer_id)" },
    ],
  });

  assert.equal(
    inspector.details,
    "Input operation details: Filter: (o.amount > 900.00) · Single-row index lookup: on c using PRIMARY (id=o.customer_id)",
  );
  assert.doesNotMatch(inspector.details, /node_\d+/i);
});

test("preserves raw details for scans, lookups, joins, sorts, and aggregates", () => {
  const samples = [
    ["Table scan", "on orders"],
    ["Index lookup", "on orders using idx_customer (customer_id=7)"],
    ["Inner hash join", "(employee_id = department_id)"],
    ["Sort", "employees.salary DESC"],
    ["Group aggregate", "sum(orders.amount)"],
  ];

  for (const [operator, details] of samples) {
    assert.equal(buildNodeInspector({ operator, details }).details, details, operator);
  }
});

test("does not leave details blank when the plan reports none", () => {
  assert.match(buildNodeInspector({ operator: "Aggregate using temporary table" }).details, /no condition or additional operation details/i);
});
