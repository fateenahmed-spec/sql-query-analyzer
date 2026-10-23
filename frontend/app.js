"use strict";

const analyzeButton = document.querySelector("#analyze");
const queryInput = document.querySelector("#query");
const result = document.querySelector("#result");

analyzeButton.addEventListener("click", async () => {
  result.replaceChildren(Object.assign(document.createElement("p"), { textContent: "Analysis API will be connected in a later chunk." }));
});
