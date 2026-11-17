"use strict";

// Structural layout checks run in Node without opening a window.
// --fixture supplies the same production markup to the optional isolated renderer.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const root = path.join(__dirname, "..");
const css = fs.readFileSync(path.join(root, "style.css"), "utf8");
const markup = fs.readFileSync(path.join(root, "index.html"), "utf8");
const html = markup.replace('<link rel="stylesheet" href="style.css">', () => `<style>${css}</style>`).replace('<script src="app.js" defer></script>', "");
function checkLayout() {
  const checks = [];
  const width = window.innerWidth, height = window.innerHeight;
  const get = (id) => document.getElementById(id);
  get("camera-empty").hidden = true; get("camera-canvas").hidden = false;
  for (const mode of ["live", "dark", "error", "history", "long-result"]) {
    get("dark-banner").hidden = mode !== "dark";
    get("app-error").hidden = mode !== "error";
    get("app-error-text").textContent = "カメラの接続を確認してください。";
    if (mode === "long-result") { get("product-name").textContent = "長いメニュー名 ".repeat(20); get("ocr-text").textContent = "読み取った文字\n".repeat(40); }
    get("detection-history").replaceChildren();
    if (["history", "long-result"].includes(mode)) for (let i = 0; i < 80; i++) { const row = document.createElement("li"); row.textContent = "12:34:56 テスト用メニュー 350円"; get("detection-history").append(row); }
    const preview = get("preview").getBoundingClientRect(), settings = get("settings-panel").getBoundingClientRect();
    const proof = { width, height, mode, horizontalOverflow: document.documentElement.scrollWidth > width + 1, previewHeight: preview.height };
    if (proof.horizontalOverflow || preview.height < 150 || settings.width < 230) throw new Error(JSON.stringify(proof));
    if (width >= 680 && settings.left < preview.right) throw new Error("Controls must sit beside the preview");
    if (width < 680 && settings.top < preview.bottom) throw new Error("Narrow controls must stack below the preview");
    if (["history", "long-result"].includes(mode) && get("detection-history").scrollHeight <= get("detection-history").clientHeight) throw new Error("History must scroll internally");
    checks.push(proof);
  }
  return checks;
}
if (process.argv.includes("--fixture")) console.log(JSON.stringify({ html, expression: `JSON.stringify((${checkLayout.toString()})())` }));
else {
  assert.match(markup, /<aside id="settings-panel"/);
  assert(markup.indexOf('id="app-error"') < markup.indexOf('<main'), "Global errors must not displace the preview's grid row");
  assert(!/<dialog|settings-open|settings-apply|撮影して認識/.test(markup));
  assert.match(css, /grid-template-columns:minmax\(0,1fr\) minmax\(250px,300px\)/);
  assert.match(css, /\.settings-panel\{grid-column:2;grid-row:1\/3;position:sticky/);
  assert.match(css, /@media\(max-width:679px\)/);
  assert.match(css, /\.settings-panel\{grid-column:1;grid-row:auto;position:static;max-height:none/);
  assert.match(css, /\.detection-history\{[^}]*max-height:260px;overflow:auto/);
  assert.match(css, /pre\{[^}]*white-space:pre-wrap;overflow-wrap:anywhere/);
  assert.match(css, /\.workspace>\*\{min-width:0\}/);
  assert.match(markup, /aria-label="検出履歴（新しい順）"/);
  console.log("PASS layout: persistent adjacent controls, narrow stacked layout, scrollable history/OCR, long-text wrapping, no shot/drawer flow (structural HTML/CSS checks; no GUI)");
}
