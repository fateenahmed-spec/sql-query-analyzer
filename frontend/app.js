"use strict";

cytoscape.use(cytoscapeDagre);

let cy = null;
let globalData = null;
let currentPlan = null;
let animSteps = [];
let currentIndex = -1;
let animTimer = null;
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

function resultSurvivorIds(sourceSamples) {
  const tableNames = Object.keys(sourceSamples || {});
  if (tableNames.length !== 1 || !globalData?.columns?.includes("id")) return null;
  return new Set((globalData.rows || []).map((row) => row.id).filter((id) => id !== undefined && id !== null).map(String));
}

function renderSourceTables() {
  const container = document.querySelector("#source-tables");
  container.replaceChildren();
  const sourceSamples = globalData?.source_samples || {};
  const survivorIds = resultSurvivorIds(sourceSamples);
  const entries = Object.entries(sourceSamples);
  if (!entries.length) {
    appendEmpty(container, demoOffline ? "Offline plan examples include no live table rows." : "No source table samples were returned.");
    return;
  }
  for (const [tableName, rows] of entries) {
    renderTable(container, tableName, rows || [], { tableName, survivorIds });
  }
}

function renderIntermediateStats() {
  const container = document.querySelector("#intermediate-table");
  container.replaceChildren();
  const nodes = planNodes(globalData?.tree);
  if (!nodes.length) {
    appendEmpty(container, "No per-operator MySQL statistics are available for this error response.");
    return;
  }
  for (const node of nodes) {
    const stat = document.createElement("article");
    stat.className = "node-stat";
    stat.dataset.nodeId = node.id;
    const name = document.createElement("strong");
    name.textContent = node.operator || "Operator";
    name.title = node.details || "";
    const input = document.createElement("span");
    const inputRows = node.children?.length
      ? node.children.reduce((sum, child) => sum + Number(child.rows_total || 0), 0)
      : Number(node.rows_total || 0);
    input.textContent = `${inputRows} → ${node.rows_total ?? 0} rows`;
    input.title = "Rows examined → rows produced (from the execution plan)";
    const loops = document.createElement("span");
    loops.textContent = `${node.loops ?? 0} loops`;
    const time = document.createElement("span");
    time.textContent = timeLabel(node.self_time_ms);
    stat.append(name, input, loops, time);
    container.append(stat);
  }
}

function renderFinalResults() {
  const container = document.querySelector("#final-results");
  container.replaceChildren();
  const rows = globalData?.rows || [];
  const rootReached = currentPlan && currentIndex >= 0 && animSteps[currentIndex]?.node_id === currentPlan.id;
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

function updateGraphVisuals() {
  if (cy) cy.nodes().removeClass("current-node");
  const sourceCards = document.querySelectorAll(".table-card[data-table-name]");
  for (const card of sourceCards) card.classList.remove("is-scanning");
  for (const stat of document.querySelectorAll(".node-stat")) stat.classList.remove("current-stat");

  if (currentIndex >= 0 && currentIndex < animSteps.length) {
    const step = animSteps[currentIndex];
    if (cy) {
      const active = cy.getElementById(step.node_id);
      if (active.nonempty()) active.addClass("current-node");
    }
    const activeStat = document.querySelector(`.node-stat[data-node-id="${CSS.escape(step.node_id)}"]`);
    if (activeStat) activeStat.classList.add("current-stat");
    if (step.action === "scan") {
      const tableName = sourceTableForNode(step.node_id);
      for (const card of sourceCards) {
        if (card.dataset.tableName === tableName) card.classList.add("is-scanning");
      }
    }
    stepStatus.textContent = `Step ${currentIndex + 1} of ${animSteps.length}: ${String(step.action || "other").toUpperCase()} (${step.rows_out ?? 0} rows)`;
    stepProgress.max = Math.max(animSteps.length, 1);
    stepProgress.value = currentIndex + 1;
  } else {
    stepStatus.textContent = `Ready (0 / ${animSteps.length})`;
    stepProgress.max = Math.max(animSteps.length, 1);
    stepProgress.value = 0;
  }
  renderFinalResults();
}

function pauseAnimation() {
  if (animTimer !== null) {
    clearInterval(animTimer);
    animTimer = null;
  }
}

function advanceAnimation() {
  if (currentIndex < animSteps.length - 1) {
    currentIndex += 1;
    updateGraphVisuals();
  }
  if (currentIndex >= animSteps.length - 1) pauseAnimation();
}

function startAnimationTimer() {
  pauseAnimation();
  if (!animSteps.length) return;
  animTimer = setInterval(advanceAnimation, Number(speedSlider.value));
}

function playAnimation() {
  if (currentIndex >= animSteps.length - 1) {
    currentIndex = -1;
    updateGraphVisuals();
  }
  startAnimationTimer();
}

function stepForward() {
  pauseAnimation();
  if (!animSteps.length) return;
  currentIndex = Math.min(animSteps.length - 1, currentIndex + 1);
  updateGraphVisuals();
}

function stepBack() {
  pauseAnimation();
  currentIndex = Math.max(-1, currentIndex - 1);
  updateGraphVisuals();
}

function resetAnimation() {
  pauseAnimation();
  currentIndex = -1;
  updateGraphVisuals();
}

function goToStep(number) {
  const target = Math.min(4, Math.max(1, Number(number)));
  if (target > 1 && !globalData) return;
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
  pauseAnimation();
  if (cy) {
    cy.destroy();
    cy = null;
  }
  currentPlan = null;
  animSteps = [];
  currentIndex = -1;
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
  globalData = data;
  demoOffline = isDemo;
  demoQueryId = queryId;
  currentPlan = data.tree || null;
  animSteps = data.steps || data.animation_steps || [];
  currentIndex = -1;
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
speedSlider.addEventListener("input", () => { if (animTimer !== null) startAnimationTimer(); });
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
