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
let joinPhaseTimer = null;
let joinPhaseResolve = null;
let currentJoinPhase = "merge";
let pauseResolvers = [];
let currentStep = 1;
let maxCompleted = 1;
let demoOffline = false;
let demoQueryId = "";
let selectedDemoQueryId = "";
let selectedCsvFile = null;
let fullFlowAutoScroll = true;
const noSampleRowsMessage = "No sample rows available for this operator (derived table or missing alias).";
const presentationDemo = new URLSearchParams(window.location.search).get("demo") === "1";

const cyContainer = document.querySelector("#cy");
const flowView = document.querySelector("#flow-view");
const mainContent = document.querySelector(".main-content");
const themeToggle = document.querySelector("#btn-theme-toggle");
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

function applyTheme(theme, save = false) {
  const isLight = theme === "light";
  document.body.classList.toggle("light-theme", isLight);
  if (themeToggle) {
    themeToggle.textContent = isLight ? "🌙 Dark" : "☀️ Light";
    themeToggle.setAttribute("aria-label", isLight ? "Switch to dark theme" : "Switch to light theme");
    themeToggle.setAttribute("aria-pressed", String(isLight));
  }
  const themeMeta = document.querySelector('meta[name="theme-color"]');
  if (themeMeta) themeMeta.content = isLight ? "#f8fafc" : "#0a0a0a";
  if (save) {
    try { localStorage.setItem("query-lens-theme", isLight ? "light" : "dark"); } catch (_) { /* Storage may be unavailable in private browsing. */ }
  }
  if (cy) cy.style(graphStyle());
}

function timeLabel(value) {
  if (value === null || value === undefined) return "—";
  return `${Number(value).toFixed(2)} ms`;
}

function flattenTree(node, parentId = null) {
  const elements = [];
  const warnings = Array.isArray(node.warnings) ? node.warnings : [];
  const operator = String(node.operator || "Operator");
  const details = String(node.details || "").trim();
  let description = operator;
  if (operator.toLowerCase() === "filter") {
    description = details ? `Filter: ${details}` : "Filter";
  } else if (operator.toLowerCase() === "table scan") {
    description = details ? `Table scan: ${details}` : "Table scan";
  } else if (details) {
    description = `${operator}: ${details}`;
  }
  const rows = node.rows_total ?? 0;
  const totalTime = node.time_total_ms ?? node.self_time_ms ?? 0;
  elements.push({
    data: {
      id: node.id,
      label: `${description}\n${rows} rows, ${totalTime}ms`,
      self_time_pct: node.self_time_pct,
      has_warnings: warnings.length > 0,
      never_executed: node.never_executed,
      dead_end: Boolean(node.dead_end),
      operator: node.operator,
      details: node.details,
      rows_total: node.rows_total,
      time_total_ms: node.time_total_ms,
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
  const lightTheme = document.body.classList.contains("light-theme");
  return [
    {
      selector: "node",
      style: {
        label: "data(label)",
        color: lightTheme ? "#0f172a" : "#ededed",
        "font-size": 11,
        "font-weight": 600,
        "font-family": "monospace",
        "text-valign": "center",
        "text-halign": "center",
        "text-wrap": "wrap",
        "text-max-width": 200,
        width: "label",
        height: "label",
        padding: "12px",
        shape: "round-rectangle",
        "background-color": (element) => {
          const percentage = Number(element.data("self_time_pct") || 0);
          const isHotspot = percentage >= 40 || Boolean(element.data("has_warnings"));
          if (isHotspot) return lightTheme ? "#fecaca" : "#7f1d1d";
          if (lightTheme) {
            if (percentage >= 20) return "#ffedd5";
            return "#f8fafc";
          }
          if (percentage >= 20) return "#7c2d12";
          return "#171717";
        },
        "border-width": 1,
        "border-color": lightTheme ? "#2563eb" : "#3b82f6",
        opacity: 1,
        "overlay-opacity": 0,
      },
    },
    { selector: "node[has_warnings = true]", style: { "border-width": 3, "border-color": lightTheme ? "#dc2626" : "#ef4444" } },
    { selector: "node[never_executed = true]", style: { opacity: 0.5, "border-style": "dashed" } },
    { selector: "node[dead_end = true]", style: { "border-width": 3, "border-color": "#c084fc", "border-style": "dashed" } },
    { selector: "node.error-node", style: { label: "data(label)", "background-color": lightTheme ? "#fee2e2" : "#7f1d1d", "border-color": "#ef4444", "border-width": 3, width: 300, height: 100, "text-wrap": "wrap", "text-max-width": 260 } },
    { selector: "node.current-node", style: { "border-color": "#fbbf24", "border-width": "5px" } },
    { selector: "node:selected", style: { "border-color": lightTheme ? "#2563eb" : "#3b82f6", "border-width": 3 } },
    {
      selector: "edge",
      style: {
        width: "mapData(rows_total, 0, 10000, 2, 8)",
        "curve-style": "bezier",
        "line-color": lightTheme ? "#2563eb" : "#3b82f6",
        "source-arrow-shape": "triangle",
        "source-arrow-color": lightTheme ? "#2563eb" : "#3b82f6",
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

function renderFlowRows(container, title, rids, states = {}, badge = "", options = {}) {
  if (!options.append) container.replaceChildren();
  if (!rids.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = options.emptyMessage || noSampleRowsMessage;
    container.append(empty);
    return;
  }

  const records = globalData?.row_records || {};
  const rows = rids.map((rid) => ({ rid, record: records[rid] })).filter((entry) => entry.record);
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = options.emptyMessage || noSampleRowsMessage;
    container.append(empty);
    return;
  }

  const card = document.createElement("article");
  card.className = `table-card${options.className ? ` ${options.className}` : ""}`;
  const tableName = rows[0].record.table || "Sample rows";
  card.dataset.tableName = tableName;
  card.dataset.alias = String(options.alias || rows[0].record.alias || "");
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

  const columns = Object.keys(rows[0].record.values || {});
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
    const rowState = states[entry.rid] || "row-pending";
    row.className = `flow-row row-scan ${rowState}`;
    row.dataset.rid = entry.rid;
    row.style.setProperty("--column-count", String(columns.length + 2));
    const stateClasses = String(rowState).split(/\s+/);
    const stateLabel = stateClasses.includes("row-pass")
      ? "PASS"
      : stateClasses.includes("row-fail")
        ? "FAIL"
        : stateClasses.includes("row-join-highlight")
          ? "MATCH"
        : stateClasses.includes("row-move")
          ? "MOVED"
          : stateClasses.includes("row-pending") ? "PENDING" : "SAMPLE";
    const stateCell = document.createElement("span");
    stateCell.className = "flow-state-label " + stateLabel.toLowerCase();
    stateCell.textContent = stateLabel === "PASS" ? "✓ PASS" : stateLabel === "FAIL" ? "× FAIL" : stateLabel;
    row.append(stateCell);
    const ridCell = document.createElement("span");
    ridCell.className = "flow-cell-rid";
    ridCell.textContent = entry.record.display_id || entry.rid;
    ridCell.title = entry.rid;
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

function sourceScanGroups() {
  const groups = [];
  const byAlias = new Map();
  const records = globalData?.row_records || {};
  animSteps.forEach((step, scanIndex) => {
    if (step.op !== "scan") return;
    const rids = (step.output_rids || []).filter((rid) => records[rid]);
    const rowRecord = records[rids[0]] || Object.values(step.row_data || {})[0] || null;
    if (!rowRecord) return;
    const alias = String(rowRecord.alias || rowRecord.table || `table_${groups.length + 1}`);
    const key = alias.toLowerCase();
    let group = byAlias.get(key);
    if (!group) {
      group = { alias, table: rowRecord.table || alias, rids: [], scanIndex, scanStep: step };
      byAlias.set(key, group);
      groups.push(group);
    }
    group.rids.push(...rids.filter((rid) => !group.rids.includes(rid)));
  });
  return groups;
}

function joinPairRids(step, pairIndex = currentRowIndex) {
  if (!step || pairIndex < 0) return [];
  const input = step.input_rids || [];
  return input.slice(pairIndex * 2, pairIndex * 2 + 2);
}

function currentJoinComparison(step) {
  if (!step || currentIndex !== animSteps.indexOf(step) || currentRowIndex < 0) return null;
  const pair = joinPairRids(step);
  if (pair.length !== 2) return null;
  const records = globalData?.row_records || {};
  const pairRecords = pair.map((rid) => records[rid]);
  if (pairRecords.some((record) => !record)) return null;
  const groups = sourceScanGroups();
  return {
    pair,
    pairRecords,
    groups,
    matched: true,
    evaluable: Boolean(step.evaluable),
    condition: String(step.note || "JOIN equality"),
  };
}

function joinStatesForGroup(group, step) {
  const states = {};
  if (!step || currentIndex < animSteps.indexOf(step) || !step.evaluable) return states;
  const matchedRids = new Set(step.input_rids || []);
  const completedRids = new Set();
  const activePair = currentIndex === animSteps.indexOf(step) ? joinPairRids(step) : [];
  const lastPair = currentIndex === animSteps.indexOf(step)
    ? Math.min(currentRowIndex, Math.floor((step.input_rids || []).length / 2) - 1)
    : Math.floor((step.input_rids || []).length / 2) - 1;
  for (let pairIndex = 0; pairIndex <= lastPair; pairIndex += 1) {
    for (const rid of joinPairRids(step, pairIndex)) completedRids.add(rid);
  }
  for (const rid of group.rids) {
    if (!matchedRids.has(rid)) states[rid] = "row-fail row-dim";
    else if (!completedRids.has(rid)) states[rid] = "row-pending";
    else if (currentJoinPhase === "highlight" && activePair.includes(rid) && currentIndex === animSteps.indexOf(step)) {
      states[rid] = "row-active row-join-highlight";
    } else if (activePair.includes(rid) && currentIndex === animSteps.indexOf(step)) {
      states[rid] = "row-active row-pass row-move";
    } else states[rid] = "row-pass";
  }
  return states;
}

function sourceGroupStates(group) {
  if (currentIndex < 0) return {};
  const currentStep = animSteps[currentIndex];
  if (currentStep?.op === "join") return joinStatesForGroup(group, currentStep);
  const currentRids = new Set(flowInputRids(currentStep));
  if (currentStep?.op === "scan" && currentIndex === group.scanIndex) return currentStepStates(currentStep);
  if (group.rids.some((rid) => currentRids.has(rid))) return currentStepStates(currentStep);

  const scanLastEvent = lastEventForStep(group.scanIndex);
  if (scanLastEvent >= 0 && animationEventIndex >= scanLastEvent) {
    return Object.fromEntries(group.rids.map((rid) => [rid, "row-pass"]));
  }
  return {};
}

function renderSourceTables() {
  const container = document.querySelector("#source-tables");
  const step = currentIndex >= 0 ? animSteps[currentIndex] : animSteps[0];
  if (!step) {
    appendEmpty(container, demoOffline ? "Offline plans have no live rows to animate." : "No real row sample was returned.");
    return;
  }

  const groups = sourceScanGroups();
  const showJoinSources = /\bjoin\b/i.test(String(globalData?.query || "")) && groups.length > 1;
  if (showJoinSources) {
    container.replaceChildren();
    groups.forEach((group, index) => {
      if (index > 0) {
        const badge = document.createElement("div");
        badge.className = "join-source-badge";
        badge.textContent = "JOIN";
        container.append(badge);
      }
      const tableAlias = group.alias ? ` (${group.alias})` : "";
      const title = `Table ${String.fromCharCode(65 + index)}: ${group.table}${tableAlias}`;
      const sourceClass = index === 0 ? "table-source-a" : "table-source-b";
      renderFlowRows(container, title, group.rids, sourceGroupStates(group), "", {
        append: true,
        className: sourceClass,
        alias: group.alias,
        emptyMessage: `${noSampleRowsMessage} Source ${group.alias} has no sampled rows.`,
      });
    });
    return;
  }

  const rids = flowInputRids(step);
  const firstRecord = (globalData?.row_records || {})[rids[0]];
  const sourceAlias = firstRecord?.alias ? ` (${firstRecord.alias})` : "";
  const title = firstRecord?.table
    ? `Source table: ${firstRecord.table}${sourceAlias}`
    : step.op === "scan" ? `Source scan - ${step.label}` : `Input rows - ${step.label}`;
  if (!rids.length && !step.never_executed) {
    appendEmpty(container, `${noSampleRowsMessage} Full run reported ${formattedCount(step.actual_rows)} rows.`);
    return;
  }
  renderFlowRows(container, title, rids, currentIndex >= 0 ? currentStepStates(step) : {});
}

function renderIntermediateStats() {
  const container = document.querySelector("#intermediate-table");
  const step = currentIndex >= 0 ? animSteps[currentIndex] : null;
  if (step?.op === "join") {
    renderJoinIntermediate(container, step);
    return;
  }
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
  const outputRids = currentIntermediateRids(step);
  const outputStates = Object.fromEntries(outputRids.map((rid) => [rid, "row-pass row-move"]));
  renderFlowRows(container, `Output rows - ${step.label}`, outputRids, outputStates, badge);
}

function renderJoinIntermediate(container, step) {
  const comparison = currentJoinComparison(step);
  if (!comparison) {
    appendEmpty(container, step.never_executed
      ? "JOIN did not run because an upstream input returned 0 rows."
      : step.evaluable && currentIndex === animSteps.indexOf(step)
        ? "No sampled input pair matched the JOIN condition. Unmatched source rows are marked red."
        : !step.evaluable && currentIndex === animSteps.indexOf(step)
          ? step.note || "The JOIN condition could not be mapped to sampled rows."
          : "Matching rows appear here as each JOIN pair is evaluated.");
    return;
  }
  if (!comparison.evaluable) {
    appendEmpty(container, step.note || "The JOIN condition could not be mapped to sampled rows.");
    return;
  }
  if (currentJoinPhase === "highlight") {
    appendEmpty(container, "Matching input rows highlighted. Moving the pair to Intermediate…");
    return;
  }

  const records = globalData.row_records;
  const sides = comparison.pair.map((rid, index) => ({
    alias: String(comparison.pairRecords[index].alias || comparison.pairRecords[index].table || `Table ${index + 1}`),
    rid,
    record: comparison.pairRecords[index],
  }));
  const displaySides = sides.map((side) => {
    const rowNumber = String(side.rid).match(/#(\d+)$/)?.[1] || "?";
    return `${side.alias}#${rowNumber}`;
  });
  container.replaceChildren();
  const values = {};
  for (const side of sides) {
    for (const [column, value] of Object.entries(side.record.values || {})) {
      values[`${side.alias}.${column}`] = value;
    }
  }
  const rowId = `__ui_join_${String(step.node_id || "join").replace(/[^\w-]/g, "_")}_${animationEventIndex}`;
  records[rowId] = {
    table: "Joined rows",
    alias: sides.map((side) => side.alias).join("+"),
    display_id: displaySides.join(" + "),
    values,
  };
  renderFlowRows(container, `Joined pair - ${sides.map((side) => side.alias).join(" + ")}`, [rowId], {
    [rowId]: "row-active row-pass row-move",
  }, "Matched by JOIN condition");
  delete records[rowId];
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

function shortPipelineRid(rid, record, index) {
  const match = String(rid || "").match(/#(\d+)$/);
  if (match) {
    const alias = record?.alias;
    const table = String(record?.table || "row").split(".").pop();
    return `${alias || table}#${match[1]}`;
  }
  const alias = record?.alias;
  const table = String(record?.table || String(rid || "row")).split(".").pop();
  return `${alias || table}#${index + 1}`;
}

function pipelineCell(text, classes = "", title = "") {
  const cell = document.createElement("div");
  cell.className = `pipeline-cell${classes ? ` ${classes}` : ""}`;
  cell.textContent = text;
  if (title) cell.title = title;
  return cell;
}

function pipelineTable(title, rows, options = {}) {
  const block = document.createElement("section");
  block.className = `pipeline-table-block${options.result ? " pipeline-result-table" : ""}`;
  const heading = document.createElement("div");
  heading.className = "pipeline-table-title";
  const headingName = document.createElement("strong");
  headingName.textContent = title;
  const headingCount = document.createElement("span");
  headingCount.textContent = `${rows.length} ${rows.length === 1 ? "row" : "rows"}`;
  heading.append(headingName, headingCount);
  block.append(heading);

  const dataRows = rows.filter((row) => row && row.record && typeof row.record === "object");
  if (!dataRows.length) {
    const empty = document.createElement("p");
    empty.className = "pipeline-empty";
    empty.textContent = options.emptyMessage || noSampleRowsMessage;
    block.append(empty);
    return block;
  }
  const columns = Object.keys(dataRows[0].record.values || {});
  const wrap = document.createElement("div");
  wrap.className = "pipeline-table-wrap";
  const table = document.createElement("div");
  table.className = `pipeline-table${columns.length > 8 ? " has-many-columns" : ""}`;
  table.style.setProperty("--flow-column-count", String(columns.length));
  table.setAttribute("role", "table");
  if (!options.result) table.append(pipelineCell("State", "is-header is-state"), pipelineCell("Row ID", "is-header is-rid"));
  for (const column of columns) table.append(pipelineCell(column, "is-header", column));

  dataRows.forEach((entry, index) => {
    const record = entry.record;
    const values = record.values || {};
    const state = entry.state || "sample";
    const row = document.createElement("div");
    row.className = `pipeline-row${state === "pass" ? " flow-pass" : ""}${state === "fail" ? " flow-fail" : ""}${entry.active ? " flow-active" : ""}${entry.collapsed ? " flow-collapsed" : ""}`;
    row.setAttribute("role", "row");
    const stateText = state === "pass" ? "✓ PASS" : state === "fail" ? "× FAIL" : state === "result" ? "RESULT" : "SAMPLE";
    if (!options.result) {
      row.append(pipelineCell(stateText, `is-state${state === "fail" ? " is-null" : ""}`));
      row.append(pipelineCell(shortPipelineRid(entry.rid, record, index), "is-rid", String(entry.rid || "")));
    }
    for (const column of columns) {
      const value = values[column];
      const text = displayValue(value);
      const numeric = typeof value === "number" || (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value)));
      const classes = `${value === null || value === undefined ? "is-null" : ""}${numeric ? " is-number" : ""}`.trim();
      row.append(pipelineCell(text, classes, text));
    }
    table.append(row);
    if (entry.reason && state === "fail") {
      const reason = document.createElement("div");
      reason.className = "pipeline-reason";
      reason.textContent = entry.reason;
      table.append(reason);
    }
  });
  wrap.append(table);
  block.append(wrap);
  return block;
}

function pipelineConditionReason(step, record, passed) {
  const condition = String(step.condition || step.label || "").trim();
  const match = condition.match(/(?:`?[\w$]+`?\s*\.\s*)*`?([\w$]+)`?\s*(>=|<=|<>|!=|=|>|<)\s*(?:'([^']*)'|"([^"]*)"|(-?\d+(?:\.\d+)?))/i);
  if (!match) return step.note || `Unsupported condition: cannot evaluate ${condition || "this operator"} from the sample values.`;
  const column = match[1];
  const operator = match[2];
  const expected = match[3] ?? match[4] ?? match[5];
  const key = Object.keys(record?.values || {}).find((candidate) => candidate.toLowerCase() === column.toLowerCase());
  if (!key) return `Cannot explain ${condition}: column ${column} is missing from this sample row.`;
  const actual = displayValue(record.values[key]);
  return passed
    ? `${column} ${actual} satisfies ${operator} ${expected}`
    : `${column} ${actual} is not ${operator} ${expected}`;
}

function pipelineStepRows(step, stepIndex) {
  const records = globalData?.row_records || {};
  const outputs = new Set(step.output_rids || []);
  return flowInputRids(step).map((rid, rowIndex) => {
    const record = records[rid];
    if (!record) return null;
    const eventIndex = animationEvents.findIndex((event) => event.stepIndex === stepIndex && event.rowIndex === rowIndex);
    const processed = eventIndex >= 0 && animationEventIndex >= eventIndex;
    const active = processed && animationEventIndex === eventIndex;
    const passed = step.row_results?.[rid] ?? outputs.has(rid);
    const state = processed ? (passed ? "pass" : "fail") : "sample";
    return {
      rid,
      record,
      state,
      active,
      collapsed: state === "fail" && !active,
      reason: step.op === "filter" && !passed ? pipelineConditionReason(step, record, false) : "",
    };
  }).filter(Boolean);
}

function pipelineCard(step, stepIndex) {
  const card = document.createElement("article");
  card.className = "pipeline-card";
  card.dataset.stepIndex = String(stepIndex);
  if (currentIndex === stepIndex) card.classList.add("is-active");

  const heading = document.createElement("header");
  heading.className = "pipeline-card-heading";
  const titleGroup = document.createElement("div");
  const title = document.createElement("h2");
  title.textContent = step.label || step.op || "Query operator";
  const detail = document.createElement("p");
  detail.textContent = `${String(step.op || "operator").toUpperCase()}${step.condition ? ` · ${step.condition}` : ""}`;
  titleGroup.append(title, detail);
  const count = document.createElement("span");
  count.className = "pipeline-count";
  count.textContent = `${formattedCount((step.output_rids || []).length)} sample out`;
  heading.append(titleGroup, count);
  card.append(heading);

  const body = document.createElement("div");
  body.className = "pipeline-card-body";
  const rids = flowInputRids(step);
  const outputCount = (step.output_rids || []).length;
  const record = (globalData?.row_records || {})[rids[0]];
  const inputCount = step.op === "scan"
    ? Number(step.table_row_count || record?.table_row_count || step.actual_rows || rids.length)
    : Number(step.input_rids?.length || rids.length);
  const stats = document.createElement("div");
  stats.className = "pipeline-stat-grid";
  for (const [label, value] of [["Input rows", inputCount], ["Sample output", outputCount], ["Plan rows", Number(step.actual_rows || 0)]]) {
    const stat = document.createElement("div");
    stat.className = "pipeline-stat";
    const caption = document.createElement("span");
    caption.textContent = label;
    const statValue = document.createElement("strong");
    statValue.textContent = formattedCount(value);
    stat.append(caption, statValue);
    stats.append(stat);
  }
  body.append(stats);

  if (step.op === "join") {
    const scans = animSteps.map((candidate, index) => ({ candidate, index }))
      .filter((item) => item.candidate.op === "scan" && item.index < stepIndex)
      .slice(-2);
    if (scans.length) {
      const joinInputs = document.createElement("div");
      joinInputs.className = "pipeline-join-inputs";
      for (const item of scans) {
        const scanRows = pipelineStepRows(item.candidate, item.index);
        const sample = scanRows.map((entry) => ({ ...entry, state: "sample", active: false, collapsed: false }));
        const first = sample[0]?.record;
        const alias = first?.alias ? ` (${first.alias})` : "";
        const tableName = first?.table || item.candidate.label || "Join input";
        joinInputs.append(pipelineTable(`${tableName}${alias}`, sample, { emptyMessage: item.candidate.note || noSampleRowsMessage }));
      }
      body.append(joinInputs);
    }
  }

  if (rids.length) {
    body.append(pipelineTable("Rows at this operator", pipelineStepRows(step, stepIndex), {
      emptyMessage: step.note || noSampleRowsMessage,
    }));
  } else {
    const empty = document.createElement("p");
    empty.className = "pipeline-empty";
    empty.textContent = step.never_executed
      ? `Never executed: upstream returned 0 rows. Plan reports ${formattedCount(step.actual_rows)} output rows.`
      : step.note || `${noSampleRowsMessage} Input count ${formattedCount(inputCount)}; output count ${formattedCount(step.actual_rows || outputCount)}.`;
    body.append(empty);
  }
  if (step.note && rids.length) {
    const note = document.createElement("p");
    note.className = "pipeline-empty";
    note.textContent = step.note;
    body.append(note);
  }
  card.append(body);
  return card;
}

function createFlowToolbar() {
  const toolbar = document.createElement("section");
  toolbar.className = "flow-toolbar";
  toolbar.setAttribute("aria-label", "Full-page animation controls");
  const buttons = document.createElement("div");
  buttons.className = "playback-buttons";
  const controls = [
    ["Play", playAnimation], ["Pause", pauseAnimation], ["Step Back", stepBack],
    ["Step Forward", stepForward], ["Reset", resetAnimation],
    ["Skip to End", skipAnimationToEnd],
  ];
  for (const [label, handler] of controls) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button button-secondary";
    button.textContent = label;
    button.addEventListener("click", handler);
    buttons.append(button);
  }
  const exit = document.createElement("button");
  exit.type = "button";
  exit.className = "button button-quiet flow-exit-button";
  exit.textContent = "Back to wizard";
  exit.addEventListener("click", () => { window.location.hash = "#step-3"; });
  buttons.append(exit);
  toolbar.append(buttons);

  const controlsRow = document.createElement("div");
  controlsRow.className = "flow-toolbar-bottom";
  const autoLabel = document.createElement("label");
  autoLabel.className = "flow-auto-scroll";
  const autoScroll = document.createElement("input");
  autoScroll.type = "checkbox";
  autoScroll.checked = fullFlowAutoScroll;
  autoScroll.addEventListener("change", () => { fullFlowAutoScroll = autoScroll.checked; });
  autoLabel.append(autoScroll, document.createTextNode("Auto-scroll"));

  const speedLabel = document.createElement("label");
  speedLabel.className = "flow-speed-control";
  speedLabel.append(document.createTextNode("Speed"));
  const flowSpeed = document.createElement("input");
  flowSpeed.type = "range";
  flowSpeed.min = "300";
  flowSpeed.max = "2000";
  flowSpeed.step = "100";
  flowSpeed.value = speedSlider?.value || "1000";
  flowSpeed.addEventListener("input", () => { if (speedSlider) speedSlider.value = flowSpeed.value; });
  speedLabel.append(flowSpeed);

  const progressWrap = document.createElement("div");
  progressWrap.className = "flow-progress";
  const progress = document.createElement("progress");
  progress.max = Math.max(animationEvents.length, 1);
  progress.value = animationEventIndex + 1;
  const status = document.createElement("span");
  status.textContent = stepStatus?.textContent || `Ready (0 / ${animationEvents.length})`;
  progressWrap.append(progress, status);
  controlsRow.append(autoLabel, speedLabel, progressWrap);
  toolbar.append(controlsRow);
  return toolbar;
}

function renderFullPageFlow() {
  if (!flowView || flowView.style.display === "none") return;
  const page = document.createElement("div");
  page.className = "full-flow-page";
  const heading = document.createElement("header");
  heading.className = "full-flow-heading";
  const titleGroup = document.createElement("div");
  const eyebrow = document.createElement("p");
  eyebrow.className = "eyebrow";
  eyebrow.textContent = "QUERY EXECUTION";
  const title = document.createElement("h1");
  title.textContent = "Data flow pipeline";
  const query = document.createElement("p");
  query.className = "full-flow-query";
  query.textContent = globalData?.query || "Run a query to inspect its row-by-row data flow.";
  titleGroup.append(eyebrow, title, query);
  heading.append(titleGroup);
  page.append(heading);

  if (!globalData) {
    const empty = document.createElement("section");
    empty.className = "pipeline-card";
    const message = document.createElement("p");
    message.className = "pipeline-empty";
    message.textContent = "Run a query in Step 1 first. Its execution pipeline will appear here.";
    empty.append(message);
    page.append(empty);
    flowView.replaceChildren(page);
    return;
  }

  page.append(createFlowToolbar());
  const pipeline = document.createElement("div");
  pipeline.className = "pipeline";
  const indexedSteps = animSteps.map((step, index) => ({ step, index }));
  indexedSteps.sort((left, right) => Number(right.step.op === "scan") - Number(left.step.op === "scan") || left.index - right.index);
  indexedSteps.forEach(({ step, index }, position) => {
    pipeline.append(pipelineCard(step, index));
    const nextCount = (step.output_rids || []).length;
    const connector = document.createElement("div");
    connector.className = "pipeline-connector";
    const label = document.createElement("span");
    label.className = "pipeline-connector-label";
    label.textContent = `${formattedCount(nextCount)} sample ${nextCount === 1 ? "row" : "rows"} passed`;
    const arrow = document.createElement("span");
    arrow.className = "pipeline-connector-arrow";
    arrow.setAttribute("aria-hidden", "true");
    arrow.textContent = "↓";
    connector.append(label, arrow);
    pipeline.append(connector);
  });

  const finalCard = document.createElement("article");
  finalCard.className = "pipeline-card pipeline-final-card";
  if (currentIndex === -1 && animationEventIndex >= animationEvents.length - 1 && animationEvents.length) finalCard.classList.add("is-active");
  const finalHeading = document.createElement("header");
  finalHeading.className = "pipeline-card-heading";
  const finalTitle = document.createElement("div");
  const finalTitleText = document.createElement("h2");
  finalTitleText.textContent = "Final query result";
  const finalDetail = document.createElement("p");
  finalDetail.textContent = "Rows returned by the executed SQL query";
  finalTitle.append(finalTitleText, finalDetail);
  const finalCount = document.createElement("span");
  finalCount.className = "pipeline-count";
  finalCount.textContent = `${formattedCount((globalData.rows || []).length)} result rows`;
  finalHeading.append(finalTitle, finalCount);
  finalCard.append(finalHeading);
  const animationComplete = animationEvents.length > 0 && animationEventIndex >= animationEvents.length - 1;
  if (animationComplete) finalCard.classList.add("is-active");
  const finalBody = document.createElement("div");
  finalBody.className = "pipeline-card-body";
  const finalRows = (globalData.rows || []).map((row, index) => ({
    rid: `result#${index + 1}`,
    record: { values: row },
    state: "result",
  }));
  finalBody.append(pipelineTable("Returned rows", finalRows, {
    result: true,
    emptyMessage: demoOffline ? "Offline demo plans do not include live result rows." : "The query returned 0 rows.",
  }));
  finalCard.append(finalBody);
  pipeline.append(finalCard);
  page.append(pipeline);
  flowView.replaceChildren(page);

  if (fullFlowAutoScroll && currentIndex >= 0) {
    const activeCard = animationComplete
      ? finalCard
      : flowView.querySelector(`.pipeline-card[data-step-index="${currentIndex}"]`);
    if (activeCard) requestAnimationFrame(() => activeCard.scrollIntoView({ behavior: "smooth", block: "center" }));
  }
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
    const eventCount = step.op === "join" ? Math.floor(rids.length / 2) : rids.length;
    if (step.never_executed || !eventCount) {
      events.push({ stepIndex, rowIndex: -1 });
      continue;
    }
    for (let rowIndex = 0; rowIndex < eventCount; rowIndex += 1) {
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
  const sampleCount = step.op === "join" ? Math.floor(flowInputRids(step).length / 2) : flowInputRids(step).length;
  const outputCount = step.op === "join" ? Math.floor((step.output_rids || []).length / 2) : (step.output_rids || []).length;
  const records = globalData?.row_records || {};
  const firstRecord = [...(step.input_rids || []), ...(step.output_rids || [])]
    .map((rid) => records[rid])
    .find(Boolean);
  const tableCount = step.table_row_count || firstRecord?.table_row_count || step.actual_rows || 0;
  const scaled = step.evaluable ? "" : " Scaled from plan counts.";
  const unit = step.op === "join" ? "sample pairs matched" : "sample rows passed";
  return `${outputCount} of ${sampleCount} ${unit}. Full run: ${formattedCount(step.actual_rows)} of ${formattedCount(tableCount)} (estimated ${formattedCount(step.est_rows)}).${scaled}`;
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
        const sameAlias = record?.alias && card.dataset.alias
          ? card.dataset.alias.toLowerCase() === String(record.alias).toLowerCase()
          : card.dataset.tableName === record?.table;
        if (record && sameAlias) card.classList.add("is-scanning");
      }
    }
    if (flowEvaluationBadge) {
      flowEvaluationBadge.hidden = Boolean(step.evaluable || step.never_executed);
      flowEvaluationBadge.textContent = step.evaluable || step.never_executed ? "" : "Scaled from plan counts";
    }
    const rids = eventRowIds(step);
    const eventCount = step.op === "join" ? Math.floor(rids.length / 2) : rids.length;
    if (currentRowIndex >= 0 && currentRowIndex < eventCount) {
      const pair = step.op === "join" ? joinPairRids(step) : [];
      const record = (globalData?.row_records || {})[step.op === "join" ? pair[0] : rids[currentRowIndex]];
      const tableCount = step.table_row_count || record?.table_row_count || 0;
      stepStatus.textContent = step.op === "join"
        ? `JOIN pair ${currentRowIndex + 1} of ${eventCount}: ${pair.join(" + ")}.`
        : currentRowIndex + 1 < eventCount
          ? `Sample row ${currentRowIndex + 1} of ${eventCount} (table has ${formattedCount(tableCount)} rows).`
        : stepSummary(step);
    } else {
      stepStatus.textContent = step.never_executed
        ? "Never executed: upstream returned 0 rows."
        : step.op === "join" && step.evaluable && !flowInputRids(step).length
          ? "JOIN found no matching sampled row pairs; unmatched source rows are marked red."
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
  renderFullPageFlow();
}

function applyAnimationEvent(eventIndex, writeLog = true, joinPhase = "highlight") {
  const event = animationEvents[eventIndex];
  if (!event) return;
  if (joinPhaseTimer !== null) {
    clearTimeout(joinPhaseTimer);
    joinPhaseTimer = null;
  }
  animationEventIndex = eventIndex;
  currentIndex = event.stepIndex;
  currentRowIndex = event.rowIndex;
  const step = animSteps[currentIndex];
  currentJoinPhase = step.op === "join" && event.rowIndex >= 0 ? joinPhase : "merge";
  updateGraphVisuals();

  if (step.op === "join" && event.rowIndex >= 0) {
    const pair = joinPairRids(step, event.rowIndex);
    if (writeLog) {
      appendProcessLog(pair.length === 2
        ? `JOIN matched ${pair[0]} with ${pair[1]} using the sampled equality condition.`
        : `${step.label}: the backend did not provide a complete input row pair.`);
    }
    if (currentJoinPhase === "highlight") {
      joinPhaseTimer = window.setTimeout(() => {
        joinPhaseTimer = null;
        if (animationEventIndex !== eventIndex || currentIndex !== event.stepIndex || currentRowIndex !== event.rowIndex) return;
        currentJoinPhase = "merge";
        updateGraphVisuals();
        if (joinPhaseResolve) {
          const resolve = joinPhaseResolve;
          joinPhaseResolve = null;
          resolve();
        }
      }, 500);
    }
    const pairCount = Math.floor(flowInputRids(step).length / 2);
    if (event.rowIndex === pairCount - 1) {
      stepStatus.textContent = stepSummary(step);
      if (writeLog) appendProcessLog(`${step.label}: ${stepSummary(step)}`);
    }
    return;
  }

  if (event.rowIndex < 0) {
    if (writeLog) {
      appendProcessLog(step.never_executed
        ? "Never executed: upstream returned 0 rows."
        : step.op === "join" && step.evaluable && !flowInputRids(step).length
          ? "JOIN produced no matching sampled row pairs. Unmatched source rows are marked red."
          : !flowInputRids(step).length
          ? noSampleRowsMessage
          : step.note || `${step.label}: no sample rows to animate.`);
    }
    return;
  }

  const rid = eventRowIds(step)[event.rowIndex];
  const passed = step.row_results?.[rid] ?? (step.output_rids || []).includes(rid);
  if (writeLog) {
    if (step.op === "join") {
      const comparison = currentJoinComparison(step);
      if (comparison?.evaluable) {
        const rowNumber = String(comparison.rid).match(/#(\d+)$/)?.[1] || "?";
        appendProcessLog(comparison.matched
          ? `JOIN matched ${comparison.currentAlias}#${rowNumber} on ${comparison.condition}.`
          : `JOIN found no match for ${comparison.currentAlias}#${rowNumber} on ${comparison.condition}.`);
      } else {
        appendProcessLog(`${step.label}: ${step.note || "JOIN matching could not be inferred from the query."}`);
      }
    } else if (step.op === "filter") {
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

function waitForJoinMerge() {
  if (currentJoinPhase !== "highlight") return Promise.resolve();
  return new Promise((resolve) => { joinPhaseResolve = resolve; });
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
  if (joinPhaseTimer !== null) {
    clearTimeout(joinPhaseTimer);
    joinPhaseTimer = null;
  }
  if (joinPhaseResolve) {
    const resolve = joinPhaseResolve;
    joinPhaseResolve = null;
    resolve();
  }
  currentJoinPhase = "merge";
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
      if (nextStep?.op === "join" && nextEvent?.rowIndex >= 0) {
        await waitForJoinMerge();
        if (token !== animationRunId) break;
      }
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
    currentJoinPhase = "merge";
    updateGraphVisuals();
  }
  animationPaused = false;
  animationRunning = true;
  const token = ++animationRunId;
  void runAnimation(token);
}

function stepForward() {
  if (currentIndex >= 0 && animSteps[currentIndex]?.op === "join" && currentJoinPhase === "highlight") return;
  cancelAnimationLoop();
  if (!animationEvents.length || animationEventIndex >= animationEvents.length - 1) return;
  const nextIndex = Math.min(animationEvents.length - 1, animationEventIndex + 1);
  applyAnimationEvent(nextIndex);
}

function skipAnimationToEnd() {
  cancelAnimationLoop();
  if (!animationEvents.length) return;
  applyAnimationEvent(animationEvents.length - 1, true, "merge");
}

function stepBack() {
  cancelAnimationLoop();
  animationEventIndex = Math.max(-1, animationEventIndex - 1);
  if (animationEventIndex >= 0) {
    applyAnimationEvent(animationEventIndex, false, "merge");
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
  currentJoinPhase = "merge";
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
listen("#btn-full-page-flow", "click", () => { window.location.hash = "#flow"; });
listen("#btn-theme-toggle", "click", () => {
  applyTheme(document.body.classList.contains("light-theme") ? "dark" : "light", true);
});

function syncHashRoute() {
  const inFullFlow = window.location.hash === "#flow";
  const wasInFullFlow = document.body.classList.contains("full-flow-mode");
  document.body.classList.toggle("full-flow-mode", inFullFlow);
  if (flowView) flowView.style.display = inFullFlow ? "block" : "none";
  if (mainContent) mainContent.hidden = inFullFlow;
  if (inFullFlow) {
    renderFullPageFlow();
    return;
  }
  const requestedStep = window.location.hash.match(/^#step-([1-4])$/);
  if (requestedStep && globalData) {
    const requested = Number(requestedStep[1]);
    if (requested <= maxCompleted) goToStep(requested);
  } else if (wasInFullFlow && globalData) {
    goToStep(3);
  }
}

window.addEventListener("hashchange", syncHashRoute);

async function initialize() {
  let savedTheme = "dark";
  try {
    const preference = localStorage.getItem("query-lens-theme");
    if (preference === "light" || preference === "dark") savedTheme = preference;
  } catch (_) { /* Storage may be unavailable in private browsing. */ }
  applyTheme(savedTheme);
  populateExamples();
  goToStep(1);
  syncHashRoute();
  updateQueryCounter();
  fetchUploads();
  if (queryInput) queryInput.focus();
}

initialize();
