/**
 * verify_float_render.mjs —— 验证 WebUI「浮动」列的真实渲染分支
 *
 * 直接在 node 里执行 pages/dashboard/app.js（用最小 DOM 桩喂数据），
 * 断言 renderRecords 对 5 种边界数据的输出，确保不再出现「精确」这种
 * 把 0 值当成结论的文案。
 *
 * 用法: node tests/verify_float_render.mjs
 */
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.join(HERE, "..", "pages", "dashboard", "app.js");

const els = new Map();
function makeEl(sel) {
  const el = {
    id: sel,
    value: "",
    textContent: "",
    innerHTML: "",
    disabled: false,
    checked: false,
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {},
    removeEventListener() {},
    appendChild() {},
    setAttribute() {},
    getAttribute: () => null,
    querySelector: (s) => el2(s),
    querySelectorAll: () => [],
    focus() {},
    click() {},
  };
  return el;
}
function el2(sel) {
  if (!els.has(sel)) els.set(sel, makeEl(sel));
  return els.get(sel);
}

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  confirm: () => true,
  alert: () => {},
  localStorage: { getItem: () => null, setItem: () => {} },
  document: {
    querySelector: el2,
    querySelectorAll: () => [],
    getElementById: (id) => el2("#" + id),
    addEventListener: () => {},
    createElement: () => {
      let t = "";
      return {
        set textContent(v) { t = String(v); },
        get textContent() { return t; },
        get innerHTML() {
          return t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        },
      };
    },
  },
  window: {},
  fetch: () => Promise.reject(new Error("no network in test")),
};
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(APP, "utf8"), sandbox, { filename: APP });

if (typeof sandbox.renderRecords !== "function") {
  console.error("未拿到 renderRecords，app.js 结构可能已变");
  process.exit(2);
}

const rows = [
  // 0: 真实抽样 3.4s
  { datetime: "2026-10-17 23:13:33", schedule_name: "工作日下线", offline_time: "23:15",
    actual_trigger_minutes: 1.5, float_seconds: 3.4, float_range: 1, float_recorded: true,
    message_source: "llm", results: { success: ["G"], failed: [], skipped: [], total: 1 } },
  // 1: 未启用浮动
  { datetime: "2026-10-17 23:13:33", schedule_name: "工作日下线", offline_time: "23:15",
    actual_trigger_minutes: 2, float_seconds: 0, float_range: 0, float_recorded: true,
    message_source: "template", results: { success: ["G"], failed: [], skipped: [], total: 1 } },
  // 2: 启用但本次抽到 0
  { datetime: "2026-10-17 23:13:33", schedule_name: "工作日下线", offline_time: "23:15",
    actual_trigger_minutes: 2, float_seconds: 0, float_range: 1, float_recorded: true,
    message_source: "llm", results: { success: ["G"], failed: [], skipped: [], total: 1 } },
  // 3: 旧记录缺字段（未知）
  { datetime: "2026-07-30 15:23:02", schedule_name: "工作日下线", offline_time: "15:23",
    actual_trigger_minutes: 0.1, float_range: 1,
    message_source: "llm", results: { success: ["G"], failed: [], skipped: [], total: 1 } },
  // 4: 准点发送（actual=0.0）+ 长浮动 72s
  { datetime: "2026-10-17 23:15:00", schedule_name: "工作日下线", offline_time: "23:15",
    actual_trigger_minutes: 0, float_seconds: 72, float_range: 2, float_recorded: true,
    message_source: "llm", results: { success: ["G"], failed: [], skipped: [], total: 1 } },
  // 5: 跳过（无被动 msg_id）
  { datetime: "2026-10-17 23:15:00", schedule_name: "工作日下线", offline_time: "23:15",
    actual_trigger_minutes: 0.5, float_seconds: 1.0, float_range: 1, float_recorded: true,
    message_source: "llm", results: { success: [], failed: [], skipped: ["G"], total: 1 } },
  // 6: 旧记录：有 float_seconds=0 但当时并未采集（无 float_recorded）
  { datetime: "2026-10-16 23:13:33", schedule_name: "工作日下线", offline_time: "23:15",
    actual_trigger_minutes: 1.5, float_seconds: 0, float_range: 1,
    message_source: "llm", results: { success: ["G"], failed: [], skipped: [], total: 1 } },
];

sandbox.renderRecords(rows, rows.length);
const html = el2("#recordsBody").innerHTML;

const trs = [...html.matchAll(/<tr>([\s\S]*?)<\/tr>/g)].map((m) =>
  [...m[1].matchAll(/<td>([\s\S]*?)<\/td>/g)].map((x) =>
    x[1].replace(/<[^>]+>/g, "").trim()
  )
);

let fail = 0;
function check(name, actual, expected) {
  const ok = actual === expected;
  if (!ok) fail++;
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${name}: ${JSON.stringify(actual)}` +
    (ok ? "" : `  <- 期望 ${JSON.stringify(expected)}`));
}

console.log("=".repeat(68));
console.log("离线通知 · 浮动列渲染验证");
console.log("=".repeat(68));
console.log(`解析到 ${trs.length} 行`);

check("抽样 3.4s 显示实测值", trs[0][4], "3.4s");
check("未启用浮动不再出现「精确」", trs[1][4], "未启用");
check("启用但抽到 0 显示 0s", trs[2][4], "0s");
check("旧记录缺字段显示 —", trs[3][4], "—");
check("72s 换算为 1.2min", trs[4][4], "1.2min");
check("actual=0.0 不再变 ?min", trs[4][3], "0.0min");
check("跳过维度可见", trs[5][6], "成功 0 / 跳过 1");
check("旧记录（未采集）显示 — 而非伪造 0s", trs[6][4], "—");
check("全文已无「精确」字样", html.includes("精确") ? "有" : "无", "无");
check("旧记录带「未采集」说明 tooltip", html.includes("未采集") ? "有" : "无", "有");

console.log("\n" + (fail === 0 ? "全部通过 ✅" : `失败 ${fail} 项`));
process.exit(fail === 0 ? 0 : 1);
