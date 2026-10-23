"use strict";

cytoscape.use(cytoscapeDagre);

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
let queryLibrary = [];

const cyContainer = document.querySelector("#cy");
const queryInput = document.querySelector("#query-input");
const queryError = document.querySelector("#query-error");
const runButton = document.querySelector("#btn-run-query");
const runSpinner = document.querySelector("#run-spinner");
const runLabel = document.querySelector("#run-label");
const uploadButton = document.querySelector("#btn-upload-sql");
const sqlFileInput = document.querySelector("#sql-file-input");
const queryList = document.querySelector("#query-list");
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

const examples = {
  normal: "SELECT id, name FROM customers WHERE city = 'Mumbai' LIMIT 5",
  scan: "SELECT id, customer_id, amount FROM orders WHERE amount > 500",
  empty: "SELECT id, name FROM customers WHERE city = 'Atlantis' LIMIT 5",
};

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
        color: "#f5f7fb",
        "font-size": 11,
        "font-weight": 600,
        "font-family": "Inter, sans-serif",
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
          if (percentage >= 40) return "#a6323e";
          if (percentage >= 20) return "#9b5a24";
          return "#303844";
        },
        "border-width": 1,
        "border-color": "#485363",
        opacity: 1,
        "overlay-opacity": 0,
      },
    },
    { selector: "node[has_warnings = true]", style: { "border-width": 3, "border-color": "#e63946" } },
    { selector: "node[never_executed = true]", style: { opacity: 0.5, "border-style": "dashed" } },
    { selector: "node[dead_end = true]", style: { "border-width": 3, "border-color": "#b38af2", "border-style": "dashed" } },
    { selector: "node.error-node", style: { label: "data(label)", "background-color": "#862c39", "border-color": "#ff596d", "border-width": 3, width: 300, height: 100, "text-wrap": "wrap", "text-max-width": 260 } },
    { selector: "node.current-node", style: { "border-color": "#ffd166", "border-width": "5px" } },
    { selector: "node:selected", style: { "border-color": "#9bc0ff", "border-width": 3 } },
    {
      selector: "edge",
      style: {
        width: "mapData(rows_total, 0, 10000, 2, 8)",
        "curve-style": "bezier",
        "line-color": "#4a5568",
        "source-arrow-shape": "triangle",
        "source-arrow-color": "#4a5568",
        "target-arrow-shape": "none",
        "target-distance-from-node": 4,
        "source-distance-from-node": 4,
        opacity: 0.85,
      },
    },
  ];
}

function closeDrawer() {
  detailDrawer.hidden = true;
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
  graphMessage.hidden = true;

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
  graphMessage.textContent = message;
  graphMessage.hidden = false;
}

function warningCard(warning) {
  const card = document.createElement("article");
  const code = warning.code || warning.type || "WARNING";
  card.className = `warning-card severity-${warning.severity || "info"}${code === "DEAD_END" ? " dead-end-warning" : ""}`;
  const title = document.createElement("h3");
  title.className = "warning-title";
  title.textContent = warning.title || code;
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
  header.style.setProperty("--column-count", String(columns.length + 1));
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
    row.style.setProperty("--column-count", String(columns.length + 1));
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
  renderFlowRows(container, title, flowInputRids(step), currentIndex >= 0 ? currentStepStates(step) : {});
}

function renderIntermediateStats() {
  const container = document.querySelector("#intermediate-table");
  const step = currentIndex >= 0 ? animSteps[currentIndex] : null;
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
      stepStatus.textContent = step.never_executed ? "Never executed: upstream returned 0 rows." : step.note || "No sample rows to animate.";
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
      appendProcessLog(step.never_executed ? "Never executed: upstream returned 0 rows." : step.note || `${step.label}: no sample rows to animate.`);
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
  processLog.replaceChildren();
  closeDrawer();
  sourceBadge.textContent = "";
  planSummary.textContent = "";
  renderWarnings([]);
  queryError.hidden = true;
  queryError.textContent = "";
}

function startOver() {
  resetGraphState();
  globalData = null;
  demoOffline = false;
  demoQueryId = "";
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
  const query = queryInput.value.trim();
  queryError.hidden = true;
  queryError.textContent = "";
  if (!query) {
    queryError.textContent = "Enter a SQL query first.";
    queryError.hidden = false;
    return;
  }
  runButton.disabled = true;
  runSpinner.hidden = false;
  runLabel.textContent = "Running query…";
  try {
    const result = await getJson("/api/run-live", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    result.query = query;
    if (result.ok === false) result.error = result.error || { message: "MySQL could not execute the query." };
    loadPlanData(result, false);
  } catch (error) {
    const prefix = error.status === 400 ? "Security check: " : error.status === 503 ? "Database unavailable: " : "Could not run query: ";
    queryError.textContent = prefix + error.message;
    queryError.hidden = false;
  } finally {
    runButton.disabled = false;
    runSpinner.hidden = true;
    runLabel.textContent = "Run Query";
  }
}

async function loadOfflineDemo(id = "08_full_pipeline") {
  resetGraphState();
  queryError.hidden = true;
  try {
    const plan = await getJson(`/api/plan/${encodeURIComponent(id)}`);
    const query = plan.query || queryLibrary.find((item) => item.id === id) || { id, title: id, sql: "" };
    loadPlanData({ ...plan, query, rows: [], columns: [], source_samples: [] }, true, id);
  } catch (error) {
    queryError.textContent = `Could not load offline example: ${error.message}`;
    queryError.hidden = false;
  }
}

function populateQueries(queries) {
  queryLibrary = queries;
  queryList.replaceChildren();
  for (const query of queries) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "query-item";
    const title = document.createElement("span");
    title.className = "query-item-title";
    title.textContent = query.title;
    const sql = document.createElement("span");
    sql.className = "query-item-sql";
    sql.textContent = query.sql;
    button.append(title, sql);
    button.addEventListener("click", () => loadOfflineDemo(query.id));
    queryList.append(button);
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
for (const button of document.querySelectorAll(".example-card")) {
  button.addEventListener("click", () => { queryInput.value = examples[button.dataset.example] || ""; queryInput.focus(); });
}

runButton.addEventListener("click", runLiveQuery);
document.querySelector("#btn-load-demo").addEventListener("click", () => loadOfflineDemo());
uploadButton.addEventListener("click", () => sqlFileInput.click());
sqlFileInput.addEventListener("change", async () => {
  const file = sqlFileInput.files?.[0];
  if (!file) return;

  uploadButton.disabled = true;
  uploadButton.textContent = "Uploading...";
  const formData = new FormData();
  formData.append("file", file);
  try {
    await getJson("/api/upload-sql", { method: "POST", body: formData });
    window.alert("✅ Database populated successfully! You can now type a query and click Run Query.");
  } catch (error) {
    window.alert(`Upload failed: ${error.message}`);
  } finally {
    uploadButton.disabled = false;
    uploadButton.textContent = "Upload .sql File";
    sqlFileInput.value = "";
  }
});
document.querySelector("#btn-to-flow").addEventListener("click", () => {
  renderTables();
  goToStep(3);
  updateGraphVisuals();
});
document.querySelector("#btn-to-ai").addEventListener("click", loadAiInsights);
document.querySelector("#btn-start-over").addEventListener("click", startOver);
document.querySelector("#btn-ai-start-over").addEventListener("click", startOver);
document.querySelector("#btn-play").addEventListener("click", playAnimation);
document.querySelector("#btn-pause").addEventListener("click", pauseAnimation);
document.querySelector("#btn-step-back").addEventListener("click", stepBack);
document.querySelector("#btn-step-forward").addEventListener("click", stepForward);
document.querySelector("#btn-reset").addEventListener("click", resetAnimation);
document.querySelector("#drawer-close").addEventListener("click", closeDrawer);
document.querySelector("#query-input").addEventListener("input", () => {
  if (!queryError.hidden) {
    queryError.hidden = true;
    queryError.textContent = "";
  }
});

async function initialize() {
  try {
    const queries = await getJson("/api/queries");
    populateQueries(queries);
  } catch (error) {
    queryList.replaceChildren();
    const message = document.createElement("p");
    message.className = "muted";
    message.textContent = error.message;
    queryList.append(message);
  }
  goToStep(1);
}

initialize();
