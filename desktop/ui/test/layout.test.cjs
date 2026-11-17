"use strict";

// Layout assertions for the isolated WebKitGTK runner. No camera or Tauri runs.
const fs = require("node:fs");
const path = require("node:path");
const root = path.join(__dirname, "..");
const css = fs.readFileSync(path.join(root, "style.css"), "utf8");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8")
  .replace('<link rel="stylesheet" href="style.css">', () => `<style>${css}</style>`)
  .replace('<script src="app.js" defer></script>', "");

function checkLayout() {
  const checks = [];
  const width = window.innerWidth;
  const height = window.innerHeight;
  {
    const doc = document;
    const get = (id) => doc.getElementById(id);
    get("result-empty").hidden = false;
    get("result-data").hidden = true;
    get("ocr-details").hidden = true;
    get("camera-empty").hidden = true;
    get("camera-canvas").hidden = false;
    get("scan-button").disabled = false;
    get("app-error-text").textContent = "設定を変更できませんでした。カメラの接続を確認して再試行してください。";
    for (const mode of ["live", "dark", "error", "all-banners", "long-result"]) {
      get("dark-banner").hidden = !["dark", "all-banners", "long-result"].includes(mode);
      get("app-error").hidden = !["error", "all-banners", "long-result"].includes(mode);
      get("camera-warning").hidden = !["all-banners", "long-result"].includes(mode);
      if (mode === "long-result") {
        get("result-empty").hidden = true;
        get("result-data").hidden = false;
        get("product-name").textContent = "テスト用の長いメニュー名 ".repeat(12);
        get("result-message").textContent = "文字全体を確認してください。\n".repeat(20);
        get("ocr-details").hidden = false;
        get("ocr-details").open = true;
        get("ocr-text").textContent = "読み取った文字\n".repeat(40);
      }
      const action = get("scan-button").getBoundingClientRect();
      const preview = get("preview").getBoundingClientRect();
      const result = get("result-panel");
      const desktop = width >= 680;
      const proof = {
        width, height, mode, actionBottom: Math.round(action.bottom), previewHeight: Math.round(preview.height),
        scrollWidth: doc.documentElement.scrollWidth, scrollHeight: doc.documentElement.scrollHeight,
        viewportWidth: window.innerWidth, viewportHeight: window.innerHeight,
        horizontalOverflow: doc.documentElement.scrollWidth > width + 1,
        pageOverflow: doc.documentElement.scrollHeight > height + 1,
        resultScrolls: result.scrollHeight > result.clientHeight,
      };
      if (proof.horizontalOverflow || (desktop && proof.pageOverflow)) proof.overflowElements = [...doc.querySelectorAll("body, .workspace, .camera-section, .result-panel, .workspace-footer")].map((element) => ({ name: element.className || "body", box: window.getComputedStyle(element).boxSizing, width: element.getBoundingClientRect().width, right: element.getBoundingClientRect().right, bottom: element.getBoundingClientRect().bottom }));
      if (proof.horizontalOverflow || (desktop && (action.top < 0 || action.bottom > height || action.height < 48 || preview.height < 80 || proof.pageOverflow))) throw new Error(JSON.stringify(proof));
      if (desktop && mode === "long-result" && !proof.resultScrolls) throw new Error(`Result must scroll internally: ${JSON.stringify(proof)}`);
      if (!desktop && get("result-panel").getBoundingClientRect().top < action.bottom) throw new Error("Narrow layout must stack results after the camera action");
      checks.push(proof);
    }
  }
  return checks;
}

// The Python runner loads the real markup and changes the WebView size.
if (require.main === module) console.log(JSON.stringify({ html, expression: `JSON.stringify((${checkLayout.toString()})())` }));
