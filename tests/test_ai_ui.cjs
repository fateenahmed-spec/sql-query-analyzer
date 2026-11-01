const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("frontend/app.js", "utf8");
const css = fs.readFileSync("frontend/style.css", "utf8");
const html = fs.readFileSync("frontend/index.html", "utf8");
assert.match(css, /--polish-heading:\s*#f1f2ef/);
assert.match(css, /body\.light-theme\s*\{[\s\S]*?--polish-heading:\s*#18212a/);
assert.match(
  css,
  /\.ai-summary-block h3,[\s\S]*?\.ai-suggestions-block h3,[\s\S]*?\.ai-suggestion-block h3,[\s\S]*?\{ color: var\(--polish-heading\); \}/,
);
assert.doesNotMatch(css, /\.ai-(?:fix|sql|copy-sql)[\w-]*/);
const aiMarkup = html.slice(html.indexOf('<div id="ai-content"'), html.indexOf('<section id="ai-index-actions"'));
assert.match(aiMarkup, /<h3>What does this query do\?<\/h3>/);
assert.match(aiMarkup, /<h3>What did MySQL find\?<\/h3>/);
assert.match(aiMarkup, /<h3>AI Suggestion<\/h3>/);
assert.equal((aiMarkup.match(/<section\b/g) || []).length, 3);
assert.match(aiMarkup, /<p id="ai-suggestion"><\/p>/);
assert.doesNotMatch(aiMarkup, /Main finding|What to do next|Learning point|Improved SQL|Copy SQL|suggested-query|<pre|<code/);
assert.doesNotMatch(source, /navigator\.clipboard|ai-copy-sql|ai-suggested-query/);

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
  document: { querySelector: elementFor },
});
vm.runInContext(source.slice(start, end), context);

context.renderAiExplanation({
  summary: "This query returns matching orders.",
  finding: "MySQL used an index lookup.",
  suggestion: "If the query becomes slow, review the filter's index support.",
  suggestions: [],
  ai_status: "success",
});
assert.equal(values["#ai-summary"], "This query returns matching orders.");
assert.equal(values["#ai-finding"], "MySQL used an index lookup.");
assert.equal(values["#ai-suggestion"], "If the query becomes slow, review the filter's index support.");
assert.equal(elements.has("#ai-suggested-query"), false);

context.renderAiExplanation({
  summary: "This query returns matching orders.",
  finding: "The available plan does not show a clear performance concern.",
  ai_status: "fallback",
});
assert.equal(
  values["#ai-suggestion"],
  "No meaningful query change is needed based on the current execution plan.",
);
