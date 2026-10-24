"use strict";

if (typeof cytoscape !== "undefined" && typeof cytoscapeDagre !== "undefined") {
  cytoscape.use(cytoscapeDagre);
}

let cy = null;
let globalData = null;
let currentPlan = null;
let animSteps = [];
let currentIndex = -1;
let currentRowIndex = -1;
let animationEvents = [];
let animationEventIndex = -1;
let animationRunId = 0;
let animationRunning = false;
let animationPaused = false;
let animationSleepTimer = null;
let animationSleepResolve = null;
let pauseResolvers = [];
let currentStep = 1;
let maxCompleted = 1;
let demoOffline = false;
let demoQueryId = "";
let selectedDemoQueryId = "";
let selectedCsvFile = null;
const noSampleRowsMessage = "No sample rows available for this operator (derived table or missing alias).";
const presentationDemo = new URLSearchParams(window.location.search).get("demo") === "1";

const cyContainer = document.querySelector("#cy");
const queryInput = document.querySelector("#query-input");
const queryError = document.querySelector("#query-error");
const queryErrorMessage = document.querySelector("#query-error-message");
const queryErrorDismiss = document.querySelector("#query-error-dismiss");
const toastRegion = document.querySelector("#toast-region");
const queryCounter = document.querySelector("#query-counter");
const runButton = document.querySelector("#btn-run-query");
const runSpinner = document.querySelector("#run-spinner");
const runLabel = document.querySelector("#run-label");
const uploadButton = document.querySelector("#btn-upload-sql");
const uploadLabel = document.querySelector("#upload-label");
const sqlFileInput = document.querySelector("#sql-file-input");
const csvFileInput = document.querySelector("#csv-file-input");
const csvTableNameInput = document.querySelector("#csv-table-name");
const csvDropZone = document.querySelector("#csv-drop-zone");
const csvDropZoneText = document.querySelector("#csv-drop-zone-text");
const csvFileName = document.querySelector("#csv-file-name");
const csvUploadButton = document.querySelector("#btn-upload-csv");
const csvUploadSpinner = document.querySelector("#csv-upload-spinner");
const csvUploadLabel = document.querySelector("#csv-upload-label");
const csvUploadBanner = document.querySelector("#csv-upload-banner");
const csvSchemaPanel = document.querySelector("#csv-schema-panel");
const csvSchemaBody = document.querySelector("#csv-schema-body");
const csvSampleQueryButton = document.querySelector("#btn-use-sample-query");
const uploadedTableList = document.querySelector("#uploaded-table-list");
const examplesSelect = document.querySelector("#examples-select");
const planSummary = document.querySelector("#plan-summary");
const sourceBadge = document.querySelector("#source-badge");
const graphMessage = document.querySelector("#graph-message");
const warningCount = document.querySelector("#warning-count");
const warningsList = document.querySelector("#warnings-list");
const detailDrawer = document.querySelector("#node-detail-drawer");
const drawerWarnings = document.querySelector("#detail-warnings");
const speedSlider = document.querySelector("#speed-slider");
const stepStatus = document.querySelector("#step-status");
const stepProgress = document.querySelector("#step-progress");
const processLog = document.querySelector("#process-log");
const flowEvaluationBadge = document.querySelector("#flow-evaluation-badge");
const aiContent = document.querySelector("#ai-content");
const aiLoading = document.querySelector("#ai-loading");

const examples = [
  { id: "01_full_scan", title: "01 · Full table scan", sql: "SELECT * FROM orders WHERE amount > 500;" },
  { id: "02_index_lookup", title: "02 · Index lookup", sql: "SELECT * FROM orders WHERE customer_id = 7;" },
  { id: "03_join_no_index", title: "03 · Join without a usable index", sql: "SELECT c.city, o.status FROM customers c JOIN orders o ON c.city = o.status LIMIT 100;" },
  { id: "04_group_by", title: "04 · Group by", sql: "SELECT customer_id, SUM(amount) AS total FROM orders GROUP BY customer_id;" },
  { id: "05_sort_limit", title: "05 · Sort and limit", sql: "SELECT * FROM orders ORDER BY amount DESC LIMIT 10;" },
  { id: "06_subquery", title: "06 · Subquery", sql: "SELECT * FROM customers WHERE id IN (SELECT customer_id FROM orders WHERE amount > 990);" },
  { id: "07_zero_rows", title: "07 · Zero rows", sql: "SELECT o.id, c.name FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.amount > 5000;" },
  { id: "08_full_pipeline", title: "08 · Full pipeline", sql: "SELECT c.city, SUM(o.amount) AS total FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.amount > 100 GROUP BY c.city ORDER BY total DESC;" },
  { id: "09_slow_showcase", title: "09 · Slow showcase", sql: "SELECT * FROM orders WHERE status LIKE '%deliv%' ORDER BY created_at DESC;" },
];
const examplesById = new Map(examples.map((example) => [example.id, example]));

async function getJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    const message = payload?.error || payload?.detail || `Request failed (${response.status})`;
    const error = new Error(String(message));
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function listen(selector, eventName, handler) {
  const element = document.querySelector(selector);
  if (element) element.addEventListener(eventName, handler);
  return element;
}

function showErrorBanner(message) {
  if (!queryError || !queryErrorMessage) return;
  queryErrorMessage.textContent = String(message || "Something went wrong.");
  queryError.hidden = false;
}

function clearErrorBanner() {
  if (queryError) queryError.hidden = true;
  if (queryErrorMessage) queryErrorMessage.textContent = "";
}

function showToast(message, variant = "success") {
  if (!toastRegion) return;
  const toast = document.createElement("div");
  toast.className = `toast${variant === "warning" ? " warning" : ""}`;
  toast.setAttribute("role", "status");
  const text = document.createElement("span");
  text.textContent = String(message);
  const dismiss = document.createElement("button");
  dismiss.type = "button";
  dismiss.className = "icon-button";
  dismiss.setAttribute("aria-label", "Dismiss notification");
  dismiss.textContent = "×";
  dismiss.addEventListener("click", () => toast.remove());
  toast.append(text, dismiss);
  toastRegion.append(toast);
  window.setTimeout(() => toast.remove(), 5000);
}

function updateQueryCounter() {
  if (!queryInput || !queryCounter) return;
  const length = queryInput.value.length;
  queryCounter.hidden = length < 4500;
  queryCounter.textContent = length.toLocaleString() + " / 5,000 characters";
  queryCounter.classList.toggle("near-limit", length >= 4800);
}

function setText(selector, value) {
  const element = document.querySelector(selector);
  if (element) element.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
}

function displayValue(value) {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function timeLabel(value) {
  if (value === null || value === undefined) return "—";
  return `${Number(value).toFixed(2)} ms`;
}

function flattenTree(node, parentId = null) {
  const elements = [];
  const warnings = Array.isArray(node.warnings) ? node.warnings : [];
  elements.push({
    data: {
      id: node.id,
      label: `${node.operator || "Operator"}\n${node.rows_total ?? 0} rows`,
      self_time_pct: node.self_time_pct,
      has_warnings: warnings.length > 0,
      never_executed: node.never_executed,
      dead_end: Boolean(node.dead_end),
      operator: node.operator,
      details: node.details,
      rows_total: node.rows_total,
    },
  });
  if (parentId !== null) {
    elements.push({
      data: {
        id: `e_${parentId}_${node.id}`,
        source: parentId,
        target: node.id,
        rows_total: node.rows_total || 0,
      },
    });
  }
  for (const child of node.children || []) elements.push(...flattenTree(child, node.id));
  return elements;
}

function planNodes(root) {
  const nodes = [];
  if (!root) return nodes;
  const visit = (node) => {
    nodes.push(node);
    for (const child of node.children || []) visit(child);
  };
  visit(root);
  return nodes;
}

function graphStyle() {
  return [
    {
      selector: "node",
      style: {
        label: "data(label)",
        color: "#ededed",
        "font-size": 11,
        "font-weight": 600,
        "font-family": "system-ui, sans-serif",
        "text-valign": "center",
        "text-halign": "center",
        "text-wrap": "wrap",
        "text-max-width": 170,
        width: 190,
        height: 66,
        padding: 8,
        shape: "round-rectangle",
        "background-color": (element) => {
          const percentage = Number(element.data("self_time_pct") || 0);
          if (percentage >= 40) return "#7f1d1d";
          if (percentage >= 20) return "#7c2d12";
          return "#262626";
        },
        "border-width": 1,
        "border-color": "#525252",
        opacity: 1,
        "overlay-opacity": 0,
      },
    },
    { selector: "node[has_warnings = true]", style: { "border-width": 3, "border-color": "#ef4444" } },
    { selector: "node[never_executed = true]", style: { opacity: 0.5, "border-style": "dashed" } },
    { selector: "node[dead_end = true]", style: { "border-width": 3, "border-color": "#c084fc", "border-style": "dashed" } },
    { selector: "node.error-node", style: { label: "data(label)", "background-color": "#7f1d1d", "border-color": "#ef4444", "border-width": 3, width: 300, height: 100, "text-wrap": "wrap", "text-max-width": 260 } },
    { selector: "node.current-node", style: { "border-color": "#fbbf24", "border-width": "5px" } },
    { selector: "node:selected", style: { "border-color": "#3b82f6", "border-width": 3 } },
    {
      selector: "edge",
      style: {
        width: "mapData(rows_total, 0, 10000, 2, 8)",
        "curve-style": "bezier",
        "line-color": "#52525b",
        "source-arrow-shape": "triangle",
        "source-arrow-color": "#52525b",
        "target-arrow-shape": "none",
        "target-distance-from-node": 4,
        "source-distance-from-node": 4,
        opacity: 0.85,
      },
    },
  ];
}

function closeDrawer() {
  if (detailDrawer) detailDrawer.hidden = true;
}

function showNodeDetails(node) {
  setText("#detail-operator", node.operator);
  setText("#detail-details", node.details);
  setText("#detail-rows", node.rows_total);
  setText("#detail-estimated", node.estimated_rows);
  setText("#detail-loops", node.loops);
  setText("#detail-self-time", timeLabel(node.self_time_ms));
  setText("#detail-self-pct", `${Number(node.self_time_pct || 0).toFixed(2)}%`);
  setText("#detail-timing", `${timeLabel(node.time_first_ms)} → ${timeLabel(node.time_last_ms)}`);
  setText("#detail-execution", node.never_executed ? "Never executed" : node.dead_end ? "Dead end" : "Executed");

  drawerWarnings.replaceChildren();
  const nodeWarnings = (globalData?.warnings || []).filter((warning) => warning.node_id === node.id);
  if (nodeWarnings.length === 0) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "None";
    drawerWarnings.append(empty);
  } else {
    for (const warning of nodeWarnings) {
      const item = document.createElement("li");
      item.textContent = warning.code || warning.type || "Warning";
      item.title = warning.title || "";
      drawerWarnings.append(item);
    }
  }
  detailDrawer.hidden = false;
}

function buildGraph() {
  if (cy) {
    cy.destroy();
    cy = null;
  }
  closeDrawer();
  if (graphMessage) graphMessage.hidden = true;

  let elements;
  if (globalData?.ok === false) {
    const errorMessage = globalData.error?.message || "MySQL could not execute the query.";
    elements = [{ data: { id: "error_node", label: `ERROR\n${errorMessage}`, error_node: true }, classes: "error-node" }];
  } else {
    currentPlan = globalData?.tree || null;
    elements = currentPlan ? flattenTree(currentPlan) : [];
  }
  if (!elements.length) {
    showGraphMessage("No execution plan was returned.");
    return;
  }
  if (!cyContainer || typeof cytoscape === "undefined" || typeof cytoscapeDagre === "undefined") {
    showGraphMessage("The graph viewer is unavailable because its local dependencies could not be loaded.");
    return;
  }
  cy = cytoscape({
    container: cyContainer,
    elements,
    layout: { name: "dagre", rankDir: "TB", nodeSep: 45, rankSep: 75, edgeSep: 20 },
    style: graphStyle(),
    minZoom: 0.25,
    maxZoom: 2,
    wheelSensitivity: 0.2,
  });
  cy.on("tap", "node", (event) => {
    const node = currentPlan && findPlanNode(currentPlan, event.target.id());
    if (node) showNodeDetails(node);
  });
  cy.on("tap", (event) => {
    if (event.target === cy) closeDrawer();
  });
  cy.ready(() => {
    cy.resize();
    cy.fit(undefined, 42);
  });
}

function showGraphMessage(message) {
  if (!graphMessage) return;
  graphMessage.textContent = message;
  graphMessage.hidden = false;
}

function warningCard(warning) {
  const card = document.createElement("article");
  const code = warning.code || warning.type || "WARNING";
  card.className = `warning-card severity-${warning.severity || "info"}${code === "DEAD_END" ? " dead-end-warning" : ""}`;
  const title = document.createElement("h3");
  title.className = "warning-title";
  const severity = String(warning.severity || "info").toLowerCase();
  const severityLabel = severity === "high" ? "Error" : severity === "low" ? "Note" : severity === "info" ? "Info" : "Warning";
  title.textContent = severityLabel + ": " + (warning.title || code);
  const message = document.createElement("p");
  message.className = "warning-message";
  message.textContent = warning.message || "";
  const label = document.createElement("span");
  label.className = "warning-code";
  label.textContent = `${code} · ${warning.node_id || ""}`;
  card.append(title, message, label);
  return card;
}

function renderWarnings(warnings) {
  warningsList.replaceChildren();
  warningCount.textContent = String(warnings.length);
  if (!warnings.length) {
    const empty = document.createElement("p");
    empty.className = "warning-empty";
    empty.textContent = "No warnings found for this plan.";
    warningsList.append(empty);
  } else {
    for (const warning of warnings) warningsList.append(warningCard(warning));
  }
}

function appendEmpty(container, message) {
  const empty = document.createElement("p");
  empty.className = "empty-state";
  empty.textContent = message;
  container.replaceChildren(empty);
}

function renderTable(container, title, rows, options = {}) {
  const card = document.createElement("article");
  card.className = `table-card${options.className ? ` ${options.className}` : ""}`;
  if (options.tableName) card.dataset.tableName = options.tableName;
  const heading = document.createElement("h3");
  const name = document.createElement("span");
  name.textContent = title;
  const count = document.createElement("span");
  count.textContent = `${rows.length} ${rows.length === 1 ? "row" : "rows"}`;
  heading.append(name, count);
  card.append(heading);
  if (rows.length) {
    const scroll = document.createElement("div");
    scroll.className = "table-scroll";
    const table = document.createElement("table");
    table.className = "data-table";
    const columns = Object.keys(rows[0]);
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const column of columns) {
      const th = document.createElement("th");
      th.scope = "col";
      th.textContent = column;
      headRow.append(th);
    }
    thead.append(headRow);
    const tbody = document.createElement("tbody");
    for (const row of rows) {
      const tr = document.createElement("tr");
      if (options.survivorIds) {
        const isSurvivor = options.survivorIds.has(String(row.id));
        tr.classList.add(isSurvivor ? "source-survivor" : "source-dim");
      }
      for (const column of columns) {
        const td = document.createElement("td");
        td.textContent = displayValue(row[column]);
        td.title = td.textContent;
        tr.append(td);
      }
      tbody.append(tr);
    }
    table.append(thead, tbody);
    scroll.append(table);
    card.append(scroll);
  } else {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No sample rows are available for this table.";
    card.append(empty);
  }
  container.append(card);
}

function flowInputRids(step) {
  if (!step) return [];
  return step.op === "scan" ? (step.output_rids || []) : (step.input_rids || []);
}

function renderFlowRows(container, title, rids, states = {}, badge = "") {
  container.replaceChildren();
  if (!rids.length) {
    appendEmpty(container, "No sample rows at this operator.");
    return;
  }

  const records = globalData?.row_records || {};
  const rows = rids.map((rid) => ({ rid, record: records[rid] })).filter((entry) => entry.record);
  if (!rows.length) {
    appendEmpty(container, "No real sampled rows are available for this operator.");
    return;
  }

  const card = document.createElement("article");
  card.className = "table-card";
  const tableName = rows[0].record.table || "Sample rows";
  card.dataset.tableName = tableName;
  const heading = document.createElement("h3");
  const name = document.createElement("span");
  name.textContent = title;
  const count = document.createElement("span");
  count.textContent = `${rows.length} ${rows.length === 1 ? "row" : "rows"}`;
  heading.append(name, count);
  card.append(heading);

  if (badge) {
    const badgeNode = document.createElement("p");
    badgeNode.className = "scaled-row-badge";
    badgeNode.textContent = badge;
    card.append(badgeNode);
  }

  const columns = [...new Set(rows.flatMap((entry) => Object.keys(entry.record.values || {})))];
  const grid = document.createElement("div");
  grid.className = "row-grid";
  const header = document.createElement("div");
  header.className = "flow-row flow-row-header";
  header.style.setProperty("--column-count", String(columns.length + 2));
  const stateHeading = document.createElement("span");
  stateHeading.textContent = "State";
  header.append(stateHeading);
  const ridHeading = document.createElement("span");
  ridHeading.textContent = "Row ID";
  header.append(ridHeading);
  for (const column of columns) {
    const cell = document.createElement("span");
    cell.textContent = column;
    header.append(cell);
  }
  grid.append(header);

  for (const entry of rows) {
    const row = document.createElement("div");
    row.className = `flow-row row-scan${states[entry.rid] ? ` ${states[entry.rid]}` : ""}`;
    row.dataset.rid = entry.rid;
    row.style.setProperty("--column-count", String(columns.length + 2));
    const stateClasses = String(states[entry.rid] || "").split(/\s+/);
    const stateLabel = stateClasses.includes("row-pass")
      ? "PASS"
      : stateClasses.includes("row-fail")
        ? "FAIL"
        : stateClasses.includes("row-move")
          ? "MOVED"
          : "SAMPLE";
    const stateCell = document.createElement("span");
    stateCell.className = "flow-state-label " + stateLabel.toLowerCase();
    stateCell.textContent = stateLabel === "PASS" ? "✓ PASS" : stateLabel === "FAIL" ? "× FAIL" : stateLabel;
    row.append(stateCell);
    const ridCell = document.createElement("span");
    ridCell.className = "flow-cell-rid";
    ridCell.textContent = entry.rid;
    row.append(ridCell);
    for (const column of columns) {
      const cell = document.createElement("span");
      cell.textContent = displayValue(entry.record.values?.[column]);
      cell.title = cell.textContent;
      row.append(cell);
    }
    grid.append(row);
  }
  card.append(grid);
  container.append(card);
}

function currentStepStates(step) {
  const states = {};
  if (!step || currentRowIndex < 0) return states;
  const rids = flowInputRids(step);
  const output = new Set(step.output_rids || []);
  for (let index = 0; index <= Math.min(currentRowIndex, rids.length - 1); index += 1) {
    const rid = rids[index];
    const passed = step.row_results?.[rid] ?? output.has(rid);
    states[rid] = index === currentRowIndex
      ? `row-active ${passed ? "row-pass" : "row-fail"}`
      : passed ? "row-pass" : "row-fail";
  }
  return states;
}

function currentIntermediateRids(step) {
  if (!step || currentRowIndex < 0 || step.never_executed) return [];
  const input = flowInputRids(step);
  const processed = new Set(input.slice(0, Math.min(currentRowIndex + 1, input.length)));
  const output = new Set(step.output_rids || []);
  if (step.op === "sort") return (step.output_rids || []).filter((rid) => processed.has(rid));
  return input.filter((rid) => processed.has(rid) && output.has(rid));
}

function renderSourceTables() {
  const container = document.querySelector("#source-tables");
  const step = currentIndex >= 0 ? animSteps[currentIndex] : animSteps[0];
  if (!step) {
    appendEmpty(container, demoOffline ? "Offline plans have no live rows to animate." : "No real row sample was returned.");
    return;
  }
  const title = step.op === "scan" ? `Source scan - ${step.label}` : `Input rows - ${step.label}`;
  const rids = flowInputRids(step);
  if (!rids.length && !step.never_executed) {
    appendEmpty(container, `${noSampleRowsMessage} Full run reported ${formattedCount(step.actual_rows)} rows.`);
    return;
  }
  renderFlowRows(container, title, rids, currentIndex >= 0 ? currentStepStates(step) : {});
}

function renderIntermediateStats() {
  const container = document.querySelector("#intermediate-table");
  const step = currentIndex >= 0 ? animSteps[currentIndex] : null;
  if (step && !step.never_executed && !flowInputRids(step).length) {
    appendEmpty(container, `${noSampleRowsMessage} Full run reported ${formattedCount(step.actual_rows)} rows.`);
    return;
  }
  if (!step || currentRowIndex < 0 || step.never_executed) {
    appendEmpty(container, "Intermediate rows appear as each sample row is processed.");
    return;
  }

  if (step.op === "aggregate" && step.output_rids?.length) {
    const count = currentIntermediateRids(step).length;
    if (!count) {
      appendEmpty(container, "No aggregate output yet for the processed sample rows.");
      return;
    }
    const tokenRid = `group:${step.node_id}`;
    globalData.row_records[tokenRid] = {
      table: "Aggregate token",
      table_row_count: step.actual_rows,
      values: { token: "Sample group token", sample_rows: count },
    };
    renderFlowRows(container, "Aggregate output", [tokenRid], {}, step.evaluable ? "" : "Scaled from plan counts");
    delete globalData.row_records[tokenRid];
    return;
  }

  const badge = step.evaluable ? "" : "Scaled from plan counts";
  renderFlowRows(container, `Output rows - ${step.label}`, currentIntermediateRids(step), {}, badge);
}

function lastEventForStep(stepIndex) {
  for (let index = animationEvents.length - 1; index >= 0; index -= 1) {
    if (animationEvents[index].stepIndex === stepIndex) return index;
  }
  return -1;
}

function renderFinalResults() {
  const container = document.querySelector("#final-results");
  container.replaceChildren();
  const rows = globalData?.rows || [];
  const rootIndex = animSteps.findIndex((step) => currentPlan && step.node_id === currentPlan.id);
  const rootReached = rootIndex >= 0 && animationEventIndex >= lastEventForStep(rootIndex);
  if (!rootReached) {
    appendEmpty(container, "Final rows appear when the animation reaches the root operator.");
    return;
  }
  if (!rows.length) {
    appendEmpty(container, demoOffline ? "Offline demo plans do not include live result rows." : "The query returned 0 rows.");
    return;
  }
  renderTable(container, "Query results", rows);
}

function renderTables() {
  renderSourceTables();
  renderIntermediateStats();
  renderFinalResults();
}

function findPlanNode(node, id) {
  if (node.id === id) return node;
  for (const child of node.children || []) {
    const match = findPlanNode(child, id);
    if (match) return match;
  }
  return null;
}

function sourceTableForNode(nodeId) {
  const node = globalData?.tree && findPlanNode(globalData.tree, nodeId);
  if (!node || !node.details) return "";
  const match = String(node.details).match(/\bon\s+`?([\w$]+)/i);
  return match ? match[1] : "";
}

function buildAnimationEvents(steps) {
  const events = [];
  for (let stepIndex = 0; stepIndex < steps.length; stepIndex += 1) {
    const step = steps[stepIndex];
    const rids = flowInputRids(step);
    if (step.never_executed || !rids.length) {
      events.push({ stepIndex, rowIndex: -1 });
      continue;
    }
    for (let rowIndex = 0; rowIndex < rids.length; rowIndex += 1) {
      events.push({ stepIndex, rowIndex });
    }
  }
  return events;
}

function appendProcessLog(message) {
  if (!processLog) return;
  const entry = document.createElement("li");
  entry.textContent = String(message);
  processLog.append(entry);
  processLog.scrollTop = processLog.scrollHeight;
}

function formattedCount(value) {
  return Number(value || 0).toLocaleString();
}

function stepSummary(step) {
  const sampleCount = flowInputRids(step).length;
  const outputCount = (step.output_rids || []).length;
  const records = globalData?.row_records || {};
  const firstRecord = [...(step.input_rids || []), ...(step.output_rids || [])]
    .map((rid) => records[rid])
    .find(Boolean);
  const tableCount = step.table_row_count || firstRecord?.table_row_count || step.actual_rows || 0;
  const scaled = step.evaluable ? "" : " Scaled from plan counts.";
  return `${outputCount} of ${sampleCount} sample rows passed. Full run: ${formattedCount(step.actual_rows)} of ${formattedCount(tableCount)} (estimated ${formattedCount(step.est_rows)}).${scaled}`;
}

function eventRowIds(step) {
  return flowInputRids(step);
}

function updateGraphVisuals() {
  if (cy) cy.nodes().removeClass("current-node");
  const sourceCards = document.querySelectorAll(".table-card[data-table-name]");
  for (const card of sourceCards) card.classList.remove("is-scanning");

  if (currentIndex >= 0 && currentIndex < animSteps.length) {
    const step = animSteps[currentIndex];
    if (cy && !step.never_executed) {
      const active = cy.getElementById(step.node_id);
      if (active.nonempty()) active.addClass("current-node");
    }
    if (step.op === "scan") {
      const record = (globalData?.row_records || {})[flowInputRids(step)[0]];
      for (const card of sourceCards) {
        if (record?.table && card.dataset.tableName === record.table) card.classList.add("is-scanning");
      }
    }
    if (flowEvaluationBadge) {
      flowEvaluationBadge.hidden = Boolean(step.evaluable || step.never_executed);
      flowEvaluationBadge.textContent = step.evaluable || step.never_executed ? "" : "Scaled from plan counts";
    }
    const rids = eventRowIds(step);
    if (currentRowIndex >= 0 && currentRowIndex < rids.length) {
      const record = (globalData?.row_records || {})[rids[currentRowIndex]];
      const tableCount = step.table_row_count || record?.table_row_count || 0;
      stepStatus.textContent = currentRowIndex + 1 < rids.length
        ? `Sample row ${currentRowIndex + 1} of ${rids.length} (table has ${formattedCount(tableCount)} rows).`
        : stepSummary(step);
    } else {
      stepStatus.textContent = step.never_executed
        ? "Never executed: upstream returned 0 rows."
        : !flowInputRids(step).length
          ? `${noSampleRowsMessage} Full run reported ${formattedCount(step.actual_rows)} rows.`
          : step.note || "No sample rows to animate.";
    }
  } else {
    if (flowEvaluationBadge) {
      flowEvaluationBadge.hidden = true;
      flowEvaluationBadge.textContent = "";
    }
    stepStatus.textContent = `Ready (0 / ${animationEvents.length})`;
  }
  stepProgress.max = Math.max(animationEvents.length, 1);
  stepProgress.value = animationEventIndex + 1;
  renderTables();
}

function applyAnimationEvent(eventIndex, writeLog = true) {
  const event = animationEvents[eventIndex];
  if (!event) return;
  animationEventIndex = eventIndex;
  currentIndex = event.stepIndex;
  currentRowIndex = event.rowIndex;
  const step = animSteps[currentIndex];
  updateGraphVisuals();

  if (event.rowIndex < 0) {
    if (writeLog) {
      appendProcessLog(step.never_executed
        ? "Never executed: upstream returned 0 rows."
        : !flowInputRids(step).length
          ? noSampleRowsMessage
          : step.note || `${step.label}: no sample rows to animate.`);
    }
    return;
  }

  const rid = eventRowIds(step)[event.rowIndex];
  const passed = step.row_results?.[rid] ?? (step.output_rids || []).includes(rid);
  if (writeLog) {
    if (step.op === "filter") {
      appendProcessLog(`Filter ${step.condition || step.label}: ${rid} ${passed ? "passed" : "failed"}.`);
    } else if (step.op === "limit") {
      appendProcessLog(`Limit: ${rid} ${passed ? "retained" : "cut from"} the sample.`);
    } else if (step.op === "sort") {
      appendProcessLog(`Sort ${step.label}: placed ${rid} in the sampled order.`);
    } else {
      appendProcessLog(`${step.label}: processed sample row ${rid}${passed ? " into the output" : " as a failed sample"}.`);
    }
  }

  if (event.rowIndex === eventRowIds(step).length - 1) {
    stepStatus.textContent = stepSummary(step);
    if (writeLog) appendProcessLog(`${step.label}: ${stepSummary(step)}`);
  }
}

function settleAnimationSleep() {
  if (animationSleepTimer !== null) {
    clearTimeout(animationSleepTimer);
    animationSleepTimer = null;
  }
  if (animationSleepResolve) {
    const resolve = animationSleepResolve;
    animationSleepResolve = null;
    resolve();
  }
}

function waitForPauseGate(token) {
  if (!animationPaused || token !== animationRunId) return Promise.resolve();
  return new Promise((resolve) => pauseResolvers.push(resolve));
}

function sleep(ms, token) {
  return new Promise((resolve) => {
    animationSleepResolve = resolve;
    const speed = Math.max(Number(speedSlider.value) / 1000, 0.1);
    animationSleepTimer = setTimeout(() => {
      animationSleepTimer = null;
      animationSleepResolve = null;
      resolve();
    }, ms / speed);
  });
}

function resumeAnimation() {
  animationPaused = false;
  const waiting = pauseResolvers;
  pauseResolvers = [];
  for (const resolve of waiting) resolve();
}

function cancelAnimationLoop() {
  animationRunId += 1;
  animationRunning = false;
  animationPaused = false;
  settleAnimationSleep();
  const waiting = pauseResolvers;
  pauseResolvers = [];
  for (const resolve of waiting) resolve();
}

function pauseAnimation() {
  if (!animationRunning) return;
  animationPaused = true;
  settleAnimationSleep();
}

async function runAnimation(token) {
  try {
    while (token === animationRunId && animationEventIndex < animationEvents.length - 1) {
      await waitForPauseGate(token);
      if (token !== animationRunId) break;
      const nextEvent = animationEvents[animationEventIndex + 1];
      const nextStep = nextEvent ? animSteps[nextEvent.stepIndex] : null;
      if (nextEvent?.rowIndex >= 0 && !nextStep?.never_executed) await sleep(1000, token);
      await waitForPauseGate(token);
      if (token !== animationRunId) break;
      applyAnimationEvent(animationEventIndex + 1);
    }
  } finally {
    if (token === animationRunId) animationRunning = false;
  }
}

function playAnimation() {
  if (!animationEvents.length) return;
  if (animationRunning) {
    if (animationPaused) resumeAnimation();
    return;
  }
  if (animationEventIndex >= animationEvents.length - 1) {
    animationEventIndex = -1;
    currentIndex = -1;
    currentRowIndex = -1;
    updateGraphVisuals();
  }
  animationPaused = false;
  animationRunning = true;
  const token = ++animationRunId;
  void runAnimation(token);
}

function stepForward() {
  cancelAnimationLoop();
  if (!animationEvents.length || animationEventIndex >= animationEvents.length - 1) return;
  const nextIndex = Math.min(animationEvents.length - 1, animationEventIndex + 1);
  applyAnimationEvent(nextIndex);
}

function stepBack() {
  cancelAnimationLoop();
  animationEventIndex = Math.max(-1, animationEventIndex - 1);
  if (animationEventIndex >= 0) {
    applyAnimationEvent(animationEventIndex, false);
  } else {
    currentIndex = -1;
    currentRowIndex = -1;
    updateGraphVisuals();
  }
}

function resetAnimation() {
  cancelAnimationLoop();
  animationEventIndex = -1;
  currentIndex = -1;
  currentRowIndex = -1;
  updateGraphVisuals();
  if (processLog?.childElementCount) appendProcessLog("Animation reset to the first sample row.");
}

function goToStep(number) {
  const target = Math.min(4, Math.max(1, Number(number)));
  if (target > 1 && !globalData) return;
  if (target !== 3) cancelAnimationLoop();
  maxCompleted = Math.max(maxCompleted, target);
  currentStep = target;
  for (let stepNumber = 1; stepNumber <= 4; stepNumber += 1) {
    const view = document.querySelector(`#step-${stepNumber}`);
    view.hidden = stepNumber !== target;
    view.classList.toggle("active", stepNumber === target);
  }
  for (const button of document.querySelectorAll(".stepper-item")) {
    const stepNumber = Number(button.dataset.step);
    button.classList.toggle("active", stepNumber === target);
    button.classList.toggle("completed", stepNumber < target && stepNumber <= maxCompleted);
    button.disabled = stepNumber >= target || stepNumber > maxCompleted;
    if (stepNumber === target) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
  }
  document.querySelector("#btn-start-over").hidden = target === 1;
  if (target === 2 && cy) requestAnimationFrame(() => { cy.resize(); cy.fit(undefined, 42); });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function resetGraphState() {
  cancelAnimationLoop();
  if (cy) {
    cy.destroy();
    cy = null;
  }
  currentPlan = null;
  animSteps = [];
  animationEvents = [];
  animationEventIndex = -1;
  currentIndex = -1;
  currentRowIndex = -1;
  if (processLog) processLog.replaceChildren();
  closeDrawer();
  if (sourceBadge) sourceBadge.textContent = "";
  if (planSummary) planSummary.textContent = "";
  renderWarnings([]);
  clearErrorBanner();
}

function startOver() {
  resetGraphState();
  globalData = null;
  demoOffline = false;
  demoQueryId = "";
  selectedDemoQueryId = "";
  maxCompleted = 1;
  goToStep(1);
}

function updatePlanHeading(title, stats) {
  const heading = document.querySelector("#plan-heading");
  heading.textContent = title || "Query plan";
  if (stats) {
    planSummary.textContent = `${stats.node_count ?? 0} operators · ${Number(stats.total_time_ms || 0).toFixed(2)} ms total`;
  } else if (globalData?.ok === false) {
    planSummary.textContent = "MySQL could not execute this query. Review the error node and ask for a suggested fix.";
  } else {
    planSummary.textContent = "Live MySQL execution plan.";
  }
}

function loadPlanData(data, isDemo = false, queryId = "") {
  cancelAnimationLoop();
  globalData = data;
  demoOffline = isDemo;
  demoQueryId = queryId;
  currentPlan = data.tree || null;
  animSteps = data.row_flow || [];
  animationEvents = buildAnimationEvents(animSteps);
  animationEventIndex = -1;
  currentIndex = -1;
  currentRowIndex = -1;
  processLog.replaceChildren();
  renderWarnings(data.warnings || []);
  updatePlanHeading(data.query?.title || "Live query plan", data.stats);
  sourceBadge.textContent = isDemo ? "Offline demo plan" : data.ok === false ? "MySQL error" : "Live MySQL";
  maxCompleted = Math.max(maxCompleted, 2);
  goToStep(2);
  buildGraph();
  updateGraphVisuals();
  requestAnimationFrame(() => {
    if (cy) {
      cy.resize();
      cy.fit(undefined, 42);
    }
  });
}

async function runLiveQuery() {
  const query = queryInput?.value.trim() || "";
  clearErrorBanner();
  if (!query) {
    showErrorBanner("Enter a SQL query first.");
    return;
  }
  if (runButton) runButton.disabled = true;
  if (runSpinner) runSpinner.hidden = false;
  if (runLabel) runLabel.textContent = "Running query…";
  try {
    const result = await getJson("/api/run-live", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    result.query = query;
    if (result.ok === false) {
      const message = result.error?.message || "MySQL could not execute the query.";
      showErrorBanner("MySQL error: " + message);
      return;
    }
    loadPlanData(result, false);
  } catch (error) {
    const unavailable = error.status === 503 || error.status >= 500 || !Number.isInteger(error.status);
    if (presentationDemo && unavailable) {
      await loadOfflineDemo(selectedDemoQueryId || "08_full_pipeline");
      if (demoOffline && globalData) {
        showToast("Live database unavailable. Showing the offline demo plan.", "warning");
      }
      return;
    }
    const prefix = error.status === 400 ? "Security check: " : error.status === 503 ? "Database unavailable: " : "Could not run query: ";
    showErrorBanner(prefix + error.message);
  } finally {
    if (runButton) runButton.disabled = false;
    if (runSpinner) runSpinner.hidden = true;
    if (runLabel) runLabel.textContent = "Run Query";
  }
}

async function loadOfflineDemo(id = "08_full_pipeline") {
  resetGraphState();
  clearErrorBanner();
  try {
    const plan = await getJson(`/api/plan/${encodeURIComponent(id)}`);
    const query = plan.query || examplesById.get(id) || { id, title: id, sql: "" };
    loadPlanData({ ...plan, query, rows: [], columns: [], source_samples: [] }, true, id);
  } catch (error) {
    showErrorBanner("Could not load offline example: " + error.message);
  }
}

function populateExamples() {
  if (!examplesSelect) return;
  for (const example of examples) {
    const option = document.createElement("option");
    option.value = example.id;
    option.textContent = example.title;
    examplesSelect.append(option);
  }
}

function renderAiExplanation(explanation) {
  aiLoading.hidden = true;
  aiContent.hidden = false;
  setText("#ai-summary", explanation.summary || "No summary was returned.");
  const suggestions = document.querySelector("#ai-suggestions");
  suggestions.replaceChildren();
  const items = Array.isArray(explanation.suggestions) ? explanation.suggestions : [];
  if (!items.length) {
    const item = document.createElement("li");
    item.textContent = "No additional suggestions.";
    suggestions.append(item);
  } else {
    for (const suggestion of items) {
      const item = document.createElement("li");
      item.textContent = suggestion;
      suggestions.append(item);
    }
  }
  const fixBlock = document.querySelector("#ai-fix-block");
  const fix = document.querySelector("#ai-fix");
  fixBlock.hidden = !explanation.fix;
  fix.textContent = explanation.fix || "";
  const source = document.querySelector("#ai-source");
  source.textContent = explanation.source === "gemini" ? "Gemini" : "Rule-based (offline)";
  source.className = `ai-source ${explanation.source === "gemini" ? "gemini" : "fallback"}`;
  const note = document.querySelector("#ai-note");
  note.textContent = explanation.note || "";
  note.hidden = !explanation.note;
  const error = document.querySelector("#ai-error");
  error.textContent = explanation.error || "";
  error.hidden = !explanation.error;
}

async function loadAiInsights() {
  aiContent.hidden = true;
  aiLoading.hidden = false;
  const source = document.querySelector("#ai-source");
  source.textContent = "Preparing";
  source.className = "ai-source";
  goToStep(4);
  try {
    let explanation;
    if (demoOffline && demoQueryId) {
      explanation = await getJson(`/api/ai/${encodeURIComponent(demoQueryId)}`, { method: "POST" });
      explanation.source = "fallback";
    } else {
      explanation = await getJson("/api/ai", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query: globalData?.query || queryInput.value,
          warnings: globalData?.warnings || [],
          error: globalData?.error || null,
          tree: globalData?.tree || null,
        }),
      });
    }
    renderAiExplanation(explanation);
  } catch (error) {
    renderAiExplanation({ summary: error.message, suggestions: [], source: "fallback" });
  }
}

for (const button of document.querySelectorAll(".stepper-item")) {
  button.addEventListener("click", () => {
    const target = Number(button.dataset.step);
    if (!button.disabled && target < currentStep && target <= maxCompleted) goToStep(target);
  });
}
for (const button of document.querySelectorAll(".back-button")) {
  button.addEventListener("click", () => goToStep(Number(button.dataset.back)));
}
listen("#btn-run-query", "click", runLiveQuery);
listen("#btn-upload-sql", "click", () => sqlFileInput?.click());
listen("#sql-file-input", "change", async () => {
  const file = sqlFileInput?.files?.[0];
  if (!file) return;

  clearErrorBanner();
  if (uploadButton) uploadButton.disabled = true;
  if (uploadLabel) uploadLabel.textContent = "Uploading…";
  const formData = new FormData();
  formData.append("file", file);
  try {
    await getJson("/api/upload-sql", { method: "POST", body: formData });
    showToast("Database populated successfully. You can now type a query and click Run Query.");
  } catch (error) {
    showErrorBanner("Upload failed: " + error.message);
  } finally {
    if (uploadButton) uploadButton.disabled = false;
    if (uploadLabel) uploadLabel.textContent = "Upload .sql";
    if (sqlFileInput) sqlFileInput.value = "";
  }
});

function showCsvBanner(message, success = false) {
  if (!csvUploadBanner) return;
  csvUploadBanner.textContent = String(message || "Something went wrong.");
  csvUploadBanner.classList.toggle("success", success);
  csvUploadBanner.setAttribute("role", success ? "status" : "alert");
  csvUploadBanner.hidden = false;
}

function clearCsvBanner() {
  if (!csvUploadBanner) return;
  csvUploadBanner.hidden = true;
  csvUploadBanner.textContent = "";
  csvUploadBanner.classList.remove("success");
  csvUploadBanner.setAttribute("role", "status");
}

function selectCsvFile(file) {
  if (!file) return;
  const isCsv = file.name.toLowerCase().endsWith(".csv");
  if (!isCsv) {
    selectedCsvFile = null;
    if (csvFileInput) csvFileInput.value = "";
    if (csvFileName) csvFileName.textContent = "No file selected";
    if (csvDropZoneText) csvDropZoneText.textContent = "Drop a CSV file here or click to browse";
    if (csvUploadButton) csvUploadButton.disabled = true;
    showCsvBanner("Choose a .csv file.");
    return;
  }
  if (file.size > 5 * 1024 * 1024) {
    selectedCsvFile = null;
    if (csvFileInput) csvFileInput.value = "";
    if (csvFileName) csvFileName.textContent = "No file selected";
    if (csvDropZoneText) csvDropZoneText.textContent = "Drop a CSV file here or click to browse";
    if (csvUploadButton) csvUploadButton.disabled = true;
    showCsvBanner("CSV files must be 5 MB or smaller.");
    return;
  }
  selectedCsvFile = file;
  if (csvFileName) csvFileName.textContent = file.name;
  if (csvDropZoneText) csvDropZoneText.textContent = file.name;
  if (csvUploadButton) csvUploadButton.disabled = false;
  clearCsvBanner();
}

function renderCsvSchema(columns) {
  if (!csvSchemaBody) return;
  csvSchemaBody.replaceChildren();
  for (const column of Array.isArray(columns) ? columns : []) {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const typeCell = document.createElement("td");
    nameCell.textContent = String(column?.name || "");
    typeCell.textContent = String(column?.type || "TEXT");
    row.append(nameCell, typeCell);
    csvSchemaBody.append(row);
  }
}

function renderUploadedTables(uploads) {
  if (!uploadedTableList) return;
  uploadedTableList.replaceChildren();
  const tables = (Array.isArray(uploads) ? uploads : []).filter((item) => /^up_[a-z0-9_]+$/i.test(String(item?.table || "")));
  if (!tables.length) {
    const empty = document.createElement("p");
    empty.className = "csv-empty-state";
    empty.textContent = "No tables uploaded yet.";
    uploadedTableList.append(empty);
    return;
  }

  for (const upload of tables) {
    const table = String(upload.table);
    const row = document.createElement("div");
    row.className = "uploaded-table-row";
    const info = document.createElement("div");
    info.className = "uploaded-table-info";
    const select = document.createElement("button");
    select.type = "button";
    select.className = "uploaded-table-select";
    select.dataset.action = "select-table";
    select.dataset.table = table;
    select.title = `Query ${table}`;
    select.textContent = table;
    const count = document.createElement("span");
    count.className = "uploaded-table-count";
    count.textContent = `${Number(upload.row_count || 0).toLocaleString()} rows`;
    info.append(select, count);

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "button button-secondary uploaded-table-delete";
    remove.dataset.action = "delete-table";
    remove.dataset.table = table;
    remove.setAttribute("aria-label", `Delete ${table}`);
    remove.textContent = "Delete";
    row.append(info, remove);
    uploadedTableList.append(row);
  }
}

async function fetchUploads() {
  if (!uploadedTableList) return;
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), 10000);
  try {
    const payload = await getJson("/api/uploads", { signal: controller.signal });
    renderUploadedTables(payload?.uploads);
  } catch (_error) {
    const message = document.createElement("p");
    message.className = "csv-empty-state";
    message.textContent = "Failed to load tables.";
    uploadedTableList.replaceChildren(message);
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function injectUploadQuery(table) {
  if (!queryInput || !/^up_[a-z0-9_]+$/i.test(String(table || ""))) return;
  queryInput.value = `SELECT * FROM analyzer_uploads.${table} LIMIT 10;`;
  selectedDemoQueryId = "";
  updateQueryCounter();
  clearErrorBanner();
  queryInput.focus();
}

async function submitCsvUpload() {
  if (!selectedCsvFile) {
    showCsvBanner("Choose a CSV file before uploading.");
    return;
  }
  if (csvUploadButton) csvUploadButton.disabled = true;
  if (csvUploadSpinner) csvUploadSpinner.hidden = false;
  if (csvUploadLabel) csvUploadLabel.textContent = "Uploading...";
  const formData = new FormData();
  const tableName = csvTableNameInput?.value.trim().replace(/\.csv$/i, "");
  formData.append("file", selectedCsvFile);
  if (tableName) formData.append("table_name", tableName);
  try {
    const payload = await getJson("/api/upload-csv", { method: "POST", body: formData });
    const table = String(payload?.table || "");
    if (!payload?.ok || !/^up_[a-z0-9_]+$/i.test(table)) {
      throw new Error("The server returned an invalid upload result.");
    }
    renderCsvSchema(payload.columns);
    if (csvSchemaPanel) csvSchemaPanel.hidden = false;
    if (csvSampleQueryButton) csvSampleQueryButton.dataset.table = table;
    showCsvBanner(`Uploaded ${table} with ${Number(payload.row_count || 0).toLocaleString()} rows.`, true);
    selectedCsvFile = null;
    if (csvFileInput) csvFileInput.value = "";
    if (csvFileName) csvFileName.textContent = "No file selected";
    if (csvDropZoneText) csvDropZoneText.textContent = "Drop a CSV file here or click to browse";
    if (csvTableNameInput) csvTableNameInput.value = "";
    await fetchUploads();
  } catch (error) {
    showCsvBanner(`Upload failed: ${error.message}`);
  } finally {
    if (csvUploadSpinner) csvUploadSpinner.hidden = true;
    if (csvUploadLabel) csvUploadLabel.textContent = "Upload CSV";
    if (csvUploadButton) csvUploadButton.disabled = !selectedCsvFile;
  }
}

listen("#csv-file-input", "change", () => selectCsvFile(csvFileInput?.files?.[0]));
listen("#btn-upload-csv", "click", submitCsvUpload);
listen("#btn-use-sample-query", "click", () => injectUploadQuery(csvSampleQueryButton?.dataset.table));
csvDropZone?.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    csvFileInput?.click();
  }
});
csvDropZone?.addEventListener("dragover", (event) => {
  event.preventDefault();
  csvDropZone.classList.add("drag-over");
});
csvDropZone?.addEventListener("dragleave", (event) => {
  if (!event.relatedTarget || !csvDropZone.contains(event.relatedTarget)) {
    csvDropZone.classList.remove("drag-over");
  }
});
csvDropZone?.addEventListener("drop", (event) => {
  event.preventDefault();
  csvDropZone.classList.remove("drag-over");
  selectCsvFile(event.dataTransfer?.files?.[0]);
});
uploadedTableList?.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const { action, table } = button.dataset;
  if (action === "select-table") {
    injectUploadQuery(table);
    return;
  }
  if (action !== "delete-table" || !/^up_[a-z0-9_]+$/i.test(String(table || ""))) return;
  button.disabled = true;
  try {
    await getJson(`/api/uploads/${encodeURIComponent(table)}`, { method: "DELETE" });
    showCsvBanner(`Deleted ${table}.`, true);
    await fetchUploads();
  } catch (error) {
    showCsvBanner(`Could not delete ${table}: ${error.message}`);
    button.disabled = false;
  }
});
listen("#examples-select", "change", () => {
  const example = examplesById.get(examplesSelect?.value);
  if (!example || !queryInput) return;
  queryInput.value = example.sql;
  selectedDemoQueryId = example.id;
  updateQueryCounter();
  examplesSelect.value = "";
  queryInput.focus();
});
listen("#query-error-dismiss", "click", clearErrorBanner);
listen("#query-input", "input", () => {
  updateQueryCounter();
  selectedDemoQueryId = examples.find((example) => example.sql.trim() === queryInput.value.trim())?.id || "";
  clearErrorBanner();
});
listen("#query-input", "keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    runLiveQuery();
  }
});
listen("#btn-to-flow", "click", () => {
  renderTables();
  goToStep(3);
  updateGraphVisuals();
});
listen("#btn-to-ai", "click", loadAiInsights);
listen("#btn-start-over", "click", startOver);
listen("#btn-ai-start-over", "click", startOver);
listen("#btn-play", "click", playAnimation);
listen("#btn-pause", "click", pauseAnimation);
listen("#btn-step-back", "click", stepBack);
listen("#btn-step-forward", "click", stepForward);
listen("#btn-reset", "click", resetAnimation);
listen("#drawer-close", "click", closeDrawer);

async function initialize() {
  populateExamples();
  goToStep(1);
  updateQueryCounter();
  fetchUploads();
  if (queryInput) queryInput.focus();
}

initialize();
