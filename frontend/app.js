"use strict";

cytoscape.use(cytoscapeDagre);

const queryList = document.querySelector("#query-list");
const queryCount = document.querySelector("#query-count");
const warningCount = document.querySelector("#warning-count");
const warningsList = document.querySelector("#warnings-list");
const aiContent = document.querySelector("#ai-content");
const graphMessage = document.querySelector("#graph-message");
const sourceBadge = document.querySelector("#source-badge");
const detailDrawer = document.querySelector("#node-detail-drawer");
const drawerWarnings = document.querySelector("#detail-warnings");
const planTitle = document.querySelector("#plan-title");
const planSummary = document.querySelector("#plan-summary");
const cyContainer = document.querySelector("#cy");
const stepStatus = document.querySelector("#step-status");
const speedSlider = document.querySelector("#speed-slider");

let cy = null;
let currentPlan = null;
let loadSequence = 0;
let animSteps = [];
let currentIndex = -1;
let animTimer = null;

function setText(selector, value) {
  const element = document.querySelector(selector);
  if (element) element.textContent = value == null || value === "" ? "—" : String(value);
}

function showGraphMessage(message) {
  graphMessage.textContent = message;
  graphMessage.hidden = false;
}

function hideGraphMessage() {
  graphMessage.hidden = true;
  graphMessage.textContent = "";
}

async function getJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (body.detail) message = body.detail;
    } catch (_) {
      // Keep the status message when the response is not JSON.
    }
    throw new Error(message);
  }
  return response.json();
}

function flattenTree(node, parentId = null) {
  const elements = [
    {
      data: {
        id: node.id,
        label: node.operator + "\n" + node.rows_total + " rows",
        self_time_pct: node.self_time_pct,
        has_warnings: node.warnings.length > 0,
        never_executed: node.never_executed,
      },
    },
  ];

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

  for (const child of node.children || []) {
    elements.push(...flattenTree(child, node.id));
  }
  return elements;
}

function timeLabel(value) {
  return value == null ? "—" : `${Number(value).toFixed(3)} ms`;
}

function closeDrawer() {
  detailDrawer.hidden = true;
}

function showNodeDetails(node) {
  setText("#drawer-title", `${node.id} · ${node.operator}`);
  setText("#detail-operator", node.operator);
  setText("#detail-details", node.details || "—");
  setText("#detail-rows", node.rows_total);
  setText("#detail-estimated", node.estimated_rows);
  setText("#detail-loops", node.loops);
  setText("#detail-self-time", timeLabel(node.self_time_ms));
  setText("#detail-self-pct", `${Number(node.self_time_pct || 0).toFixed(2)}%`);
  setText("#detail-timing", `${timeLabel(node.time_first_ms)} → ${timeLabel(node.time_last_ms)}`);
  setText("#detail-execution", node.never_executed ? "Never executed" : "Executed");

  drawerWarnings.replaceChildren();
  const nodeWarnings = (currentPlan.warnings || []).filter((warning) => warning.node_id === node.id);
  if (nodeWarnings.length === 0) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "None";
    drawerWarnings.append(empty);
  } else {
    for (const warning of nodeWarnings) {
      const item = document.createElement("li");
      item.textContent = warning.code;
      item.title = warning.title;
      drawerWarnings.append(item);
    }
  }
  detailDrawer.hidden = false;
}

function warningCard(warning) {
  const card = document.createElement("article");
  card.className = `warning-card severity-${warning.severity}`;

  const title = document.createElement("h3");
  title.className = "warning-title";
  title.textContent = warning.title;

  const message = document.createElement("p");
  message.className = "warning-message";
  message.textContent = warning.message;

  const code = document.createElement("span");
  code.className = "warning-code";
  code.textContent = `${warning.code} · ${warning.node_id}`;

  card.append(title, message, code);
  return card;
}

function renderWarnings(warnings) {
  warningsList.replaceChildren();
  warningCount.textContent = String(warnings.length);
  if (warnings.length === 0) {
    const empty = document.createElement("p");
    empty.className = "warning-empty";
    empty.textContent = "No warnings found for this plan.";
    warningsList.append(empty);
    return;
  }
  for (const warning of warnings) warningsList.append(warningCard(warning));
}

function renderAiExplanation(explanation) {
  aiContent.replaceChildren();
  const summary = document.createElement("p");
  summary.textContent = explanation.summary;
  aiContent.append(summary);

  if (explanation.suggestions.length > 0) {
    const suggestions = document.createElement("ol");
    for (const suggestion of explanation.suggestions) {
      const item = document.createElement("li");
      item.textContent = suggestion;
      suggestions.append(item);
    }
    aiContent.append(suggestions);
  }
}

async function loadAiExplanation(id, requestNumber) {
  aiContent.replaceChildren();
  const loading = document.createElement("p");
  loading.className = "muted";
  loading.textContent = "Preparing insights…";
  aiContent.append(loading);

  try {
    const explanation = await getJson(`/api/ai/${encodeURIComponent(id)}`, { method: "POST" });
    if (requestNumber !== loadSequence) return;
    renderAiExplanation(explanation);
  } catch (error) {
    if (requestNumber !== loadSequence) return;
    const message = document.createElement("p");
    message.className = "muted";
    message.textContent = `AI insights unavailable: ${error.message}`;
    aiContent.replaceChildren(message);
  }
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
    {
      selector: "node[has_warnings = true]",
      style: { "border-width": 3, "border-color": "#e63946" },
    },
    {
      selector: "node[never_executed = true]",
      style: { opacity: 0.5, "border-style": "dashed" },
    },
    {
      selector: "node.current-node",
      style: { "border-color": "#ffd166", "border-width": "5px" },
    },
    {
      selector: "node:selected",
      style: { "border-color": "#9bc0ff", "border-width": 3 },
    },
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

function buildGraph(plan) {
  if (cy) {
    cy.destroy();
    cy = null;
  }
  closeDrawer();
  hideGraphMessage();
  currentPlan = plan;

  cy = cytoscape({
    container: cyContainer,
    elements: flattenTree(plan.tree),
    layout: { name: "dagre", rankDir: "TB", nodeSep: 45, rankSep: 75, edgeSep: 20 },
    style: graphStyle(),
    minZoom: 0.25,
    maxZoom: 2,
    wheelSensitivity: 0.2,
  });

  cy.on("tap", "node", (event) => {
    const nodeId = event.target.id();
    const node = findPlanNode(plan.tree, nodeId);
    if (node) showNodeDetails(node);
  });
  cy.on("tap", (event) => {
    if (event.target === cy) closeDrawer();
  });
  cy.ready(() => cy.fit(undefined, 44));
}

function updateGraphVisuals() {
  if (cy) cy.nodes().removeClass("current-node");

  if (currentIndex >= 0 && currentIndex < animSteps.length) {
    const step = animSteps[currentIndex];
    if (cy) cy.$("#" + step.node_id).addClass("current-node");
    stepStatus.textContent = `Step ${currentIndex + 1} of ${animSteps.length}: ${step.action.toUpperCase()} (${step.rows_out} rows)`;
  } else if (currentIndex === -1) {
    stepStatus.textContent = `Ready (0 / ${animSteps.length})`;
  }
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
  animTimer = setInterval(advanceAnimation, Number(speedSlider.value));
}

function playAnimation() {
  if (currentIndex >= animSteps.length - 1) {
    currentIndex = -1;
    updateGraphVisuals();
  }
  pauseAnimation();
  startAnimationTimer();
}

function stepForward() {
  pauseAnimation();
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

function findPlanNode(node, id) {
  if (node.id === id) return node;
  for (const child of node.children || []) {
    const match = findPlanNode(child, id);
    if (match) return match;
  }
  return null;
}

function populateQueries(queries) {
  queryList.replaceChildren();
  queryCount.textContent = String(queries.length);
  for (const query of queries) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "query-item";
    button.dataset.queryId = query.id;

    const title = document.createElement("span");
    title.className = "query-item-title";
    title.textContent = query.title;

    const sql = document.createElement("span");
    sql.className = "query-item-sql";
    sql.textContent = query.sql;

    button.append(title, sql);
    button.addEventListener("click", () => loadPlan(query.id));
    queryList.append(button);
  }
}

function selectQuery(id) {
  for (const button of queryList.querySelectorAll(".query-item")) {
    button.classList.toggle("selected", button.dataset.queryId === id);
  }
}

async function loadPlan(id) {
  const requestNumber = ++loadSequence;
  pauseAnimation();
  if (cy) {
    cy.destroy();
    cy = null;
  }
  currentPlan = null;
  animSteps = [];
  currentIndex = -1;
  updateGraphVisuals();
  closeDrawer();
  selectQuery(id);
  sourceBadge.textContent = "";
  planTitle.textContent = "Loading plan…";
  planSummary.textContent = "Fetching the selected execution plan.";
  renderWarnings([]);
  void loadAiExplanation(id, requestNumber);
  showGraphMessage("Loading execution plan…");

  try {
    const plan = await getJson(`/api/plan/${encodeURIComponent(id)}`);
    if (requestNumber !== loadSequence) return;
    planTitle.textContent = plan.query.title;
    planSummary.textContent = `${plan.stats.node_count} operators · ${Number(plan.stats.total_time_ms).toFixed(2)} ms total`;
    sourceBadge.textContent = plan.source === "captured" ? "Captured plan" : "Fallback plan";
    renderWarnings(plan.warnings || []);
    animSteps = plan.animation_steps || [];
    currentIndex = -1;
    buildGraph(plan);
    updateGraphVisuals();
  } catch (error) {
    if (requestNumber !== loadSequence) return;
    planTitle.textContent = "Could not load plan";
    planSummary.textContent = error.message;
    showGraphMessage(error.message);
  }
}

async function initialize() {
  try {
    const queries = await getJson("/api/queries");
    populateQueries(queries);
    const defaultQuery = queries.find((query) => query.id === "08_full_pipeline") || queries[0];
    if (defaultQuery) {
      loadPlan(defaultQuery.id);
    } else {
      showGraphMessage("No example queries are available.");
    }
  } catch (error) {
    queryList.replaceChildren();
    const item = document.createElement("p");
    item.className = "muted loading-message";
    item.textContent = error.message;
    queryList.append(item);
    showGraphMessage(error.message);
  }
}

document.querySelector("#btn-play").addEventListener("click", playAnimation);
document.querySelector("#btn-pause").addEventListener("click", pauseAnimation);
document.querySelector("#btn-step-back").addEventListener("click", stepBack);
document.querySelector("#btn-step-forward").addEventListener("click", stepForward);
document.querySelector("#btn-reset").addEventListener("click", resetAnimation);
speedSlider.addEventListener("input", () => {
  if (animTimer !== null) startAnimationTimer();
});

document.querySelector("#drawer-close").addEventListener("click", closeDrawer);
initialize();
