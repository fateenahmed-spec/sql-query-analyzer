const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("frontend/app.js", "utf8");
const css = fs.readFileSync("frontend/style.css", "utf8");
assert.match(css, /--polish-heading:\s*#f1f2ef/);
assert.match(css, /body\.light-theme\s*\{[\s\S]*?--polish-heading:\s*#18212a/);
assert.match(
  css,
  /\.ai-summary-block h3,[\s\S]*?\.ai-suggestions-block h3,[\s\S]*?\{ color: var\(--polish-heading\); \}/,
);
assert.doesNotMatch(css, /body\.light-theme \.ai-summary-block h3/);

const start = source.indexOf("function renderAiExplanation(explanation) {");
const end = source.indexOf("\n}\n\nasync function loadAiInsights", start) + 2;
assert(start >= 0 && end > start, "renderAiExplanation should be present");

const values = {};
const elements = new Map();
const elementFor = (selector) => {
  if (!elements.has(selector)) {
    elements.set(selector, {
      hidden: false,
      textContent: "",
      className: "",
      children: [],
      replaceChildren() { this.children = []; },
      append(child) { this.children.push(child); },
      after() {},
    });
  }
  return elements.get(selector);
};
const context = vm.createContext({
  resetIndexSimulation() {},
  renderIndexSimulationActions() {},
  aiLoading: { hidden: false },
  aiContent: { hidden: true },
  setText(selector, value) {
    values[selector] = value;
    elementFor(selector).textContent = value;
  },
  document: {
    querySelector: elementFor,
    createElement() { return { textContent: "" }; },
  },
});
vm.runInContext(source.slice(start, end), context);

context.renderAiExplanation({
  summary: "First query",
  suggestions: [],
  suggested_query: {
    sql: "SELECT id FROM orders",
    kind: "general_optimization",
    why_it_may_help: "A change may help.",
    what_changed: "Simplified a predicate.",
    limitations: "Compare the results.",
  },
  ai_status: "success",
});
assert.equal(elementFor("#ai-suggested-query-content").hidden, false);
assert.equal(values["#ai-suggested-query"], "SELECT id FROM orders");

context.renderAiExplanation({
  summary: "Second query",
  suggestions: [],
  suggested_query: null,
  ai_status: "success",
});
assert.equal(elementFor("#ai-suggested-query-block").hidden, false);
assert.equal(elementFor("#ai-no-suggested-query").hidden, false);
assert.equal(elementFor("#ai-suggested-query-content").hidden, true);
assert.equal(values["#ai-suggested-query"], "");
