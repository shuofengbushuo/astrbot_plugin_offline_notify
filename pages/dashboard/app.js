/**
 * AI下线通知系统 - WebUI 管理面板
 */

// 插件 Web API 通过框架 bridge SDK 调用：window.AstrBotPluginPage.apiGet/apiPost，
// 由父窗口（WebUI 主应用）代理请求并自动附加 dashboard JWT。
// endpoint 传「相对路径」（如 "/status"），父窗口自动补全为
// /api/v1/plugins/extensions/astrbot_plugin_offline_notify/<endpoint>。
// 直接 fetch 网关会因插件页面 iframe 内拿不到 token 而 401。
//
// ⚠️ bridge 会自动剥壳：父窗口返回 (response.data) ?? response。
// 即后端返回 {"success":true,"data":X} 时，本页拿到的是 X 本身（无 success/data 外壳）；
// 后端返回 {"success":true,"message":...}（无 data 字段）时，本页拿到完整对象。
// 因此判断成功不能用 data.success，而要直接检查业务字段。

// ── DOM 元素 ──────────────────────────────────────

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// Status
const $schedulerStatus = $("#schedulerStatus");
const $jobCount = $("#jobCount");
const $triggerCount = $("#triggerCount");
const $sendStats = $("#sendStats");
const $refreshStatusBtn = $("#refreshStatusBtn");

// Preview
const $previewTime = $("#previewTime");
const $previewAdvance = $("#previewAdvance");
const $previewBtn = $("#previewBtn");
const $previewResult = $("#previewResult");

// LLM Preview
const $llmPreviewTime = $("#llmPreviewTime");
const $llmPreviewAdvance = $("#llmPreviewAdvance");
const $llmPreviewFloat = $("#llmPreviewFloat");
const $llmPreviewBtn = $("#llmPreviewBtn");
const $llmPreviewResult = $("#llmPreviewResult");
const $llmPreviewMeta = $("#llmPreviewMeta");
const $llmSourceTag = $("#llmSourceTag");
const $llmTimeInfo = $("#llmTimeInfo");

// Test
const $testGroupId = $("#testGroupId");
const $testSendBtn = $("#testSendBtn");
const $testResult = $("#testResult");

// Jobs
const $jobsList = $("#jobsList");

// Records
const $refreshRecordsBtn = $("#refreshRecordsBtn");
const $recordsCount = $("#recordsCount");
const $recordsBody = $("#recordsBody");

// Schedules
const $schedulesList = $("#schedulesList");
const $refreshSchedulesBtn = $("#refreshSchedulesBtn");
const $addScheduleBtn = $("#addScheduleBtn");
const $scheduleForm = $("#scheduleForm");
const $scheduleFormTitle = $("#scheduleFormTitle");
const $cancelScheduleBtn = $("#cancelScheduleBtn");
const $saveScheduleBtn = $("#saveScheduleBtn");
const $schedName = $("#schedName");
const $schedDayType = $("#schedDayType");
const $schedTime = $("#schedTime");
const $schedFloat = $("#schedFloat");

// Toast
const $toast = $("#toast");

// ── Toast 通知 ────────────────────────────────────

function showToast(message, type = "info") {
  $toast.textContent = message;
  $toast.className = `toast ${type}`;
  // 强制回流
  void $toast.offsetWidth;
  $toast.classList.add("show");
  setTimeout(() => {
    $toast.classList.remove("show");
  }, 3000);
}

// ── API 请求 ──────────────────────────────────────

function getBridge() {
  const b = window.AstrBotPluginPage;
  if (!b || typeof b.apiGet !== "function" || typeof b.apiPost !== "function") {
    return null;
  }
  return b;
}

async function apiGet(path, params) {
  try {
    const bridge = getBridge();
    if (!bridge) throw new Error("插件桥接未就绪");
    return await bridge.apiGet(path, params);
  } catch (err) {
    showToast(`请求失败: ${err.message}`, "error");
    return null;
  }
}

async function apiPost(path, body) {
  try {
    const bridge = getBridge();
    if (!bridge) throw new Error("插件桥接未就绪");
    return await bridge.apiPost(path, body);
  } catch (err) {
    showToast(`请求失败: ${err.message}`, "error");
    return null;
  }
}

// ── 状态刷新 ──────────────────────────────────────

async function refreshStatus() {
  $refreshStatusBtn.disabled = true;
  $refreshStatusBtn.textContent = "刷新中...";

  const [statusData, statsData] = await Promise.all([
    apiGet("/status"),
    apiGet("/stats"),
  ]);

  if (statusData) {
    renderStatus(statusData);
    renderJobs(statusData);
  }

  if (statsData && statsData.notifier) {
    renderStats(statsData.notifier);
  }

  $refreshStatusBtn.disabled = false;
  $refreshStatusBtn.textContent = "刷新状态";
  showToast("状态已刷新", "success");
}

function renderStatus(data) {
  $schedulerStatus.textContent = data.running ? "运行中" : "已停止";
  $schedulerStatus.style.color = data.running ? "#16a34a" : "#dc2626";
  $jobCount.textContent = data.job_count;
  $triggerCount.textContent = data.trigger_count;
}

function renderStats(data) {
  $sendStats.textContent = `${data.total_sent} / ${data.total_failed}`;
}

// 每个计划拆成「监听开始(open)」+「监听结束(close)」两个 cron 边界任务，二者 name 相同。
// 真正的发送完全靠窗口内命中目标群消息、借被动 msg_id 搭便车——窗口内没消息就发不出，且无任何兜底补发。
// WebUI 端按「计划名」聚合，每行显示一个计划（含监听开始 / 监听结束两个友好时间）。

// 把后端返回的 "2026-09-12 22:30:00+08:00" 格式化为 "今天 09-12 22:30"（服务器时区 +08:00）
function fmtNextRun(s) {
  if (!s) return "暂无";
  const d = new Date(String(s).replace(" ", "T"));
  if (isNaN(d.getTime())) return s;
  const pad = (n) => String(n).padStart(2, "0");
  const datePart = `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const now = new Date();
  const today0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const that0 = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diff = Math.round((that0 - today0) / 86400000);
  const rel = diff === 0 ? "今天 " : diff === 1 ? "明天 " : diff === 2 ? "后天 " : "";
  return rel + datePart;
}

function renderJobs(data) {
  if (!data.jobs || data.jobs.length === 0) {
    $jobsList.innerHTML = '<div class="empty-state">暂无定时任务</div>';
    return;
  }

  // 按计划名聚合 open / close 两个 cron
  const byName = {};
  for (const job of data.jobs) {
    const jid = job.id || "";
    const kind = job.kind ||
      (jid.includes("_open_") ? "open" : jid.includes("_close_") ? "close" : "other");
    if (!byName[job.name]) byName[job.name] = { name: job.name, open: null, close: null };
    if (kind === "open") byName[job.name].open = job;
    else if (kind === "close") byName[job.name].close = job;
  }

  $jobsList.innerHTML = Object.values(byName)
    .map((g) => `
    <div class="job-item">
      <div class="job-info">
        <div class="job-name-line">
          <span class="job-name">${escapeHtml(g.name)}</span>
          <span class="job-kind kind-open" title="按计划在窗口内监听：窗口内命中目标群消息即借被动 msg_id 发送；窗口内没消息则当天不发送，无兜底补发">计划</span>
        </div>
        <span class="job-next">监听开始: ${fmtNextRun(g.open && g.open.next_run)}</span>
        <span class="job-next">监听结束: ${fmtNextRun(g.close && g.close.next_run)}</span>
      </div>
      <span class="job-status active">活跃</span>
    </div>`)
    .join("");
}

// ── 消息预览 ──────────────────────────────────────

async function previewMessage() {
  const offlineTime = $previewTime.value || "23:00";
  const advance = parseInt($previewAdvance.value) || 5;

  $previewBtn.disabled = true;
  $previewBtn.textContent = "生成中...";

  const data = await apiPost("/preview", {
    offline_time: offlineTime,
    countdown_minutes: advance,
  });

  $previewBtn.disabled = false;
  $previewBtn.textContent = "生成预览";

  // bridge 已剥壳：data 即 {title, body, footer}（title 允许为空串，故判 body）
  if (data && typeof data.body === "string") {
    const { title, body, footer } = data;
    let html = `<div class="preview-message"><strong>${escapeHtml(title || "")}</strong>\n\n${escapeHtml(body)}`;
    if (footer) {
      html += `\n\n${escapeHtml(footer)}`;
    }
    html += "</div>";
    $previewResult.innerHTML = html;
  } else {
    $previewResult.innerHTML =
      '<div class="preview-placeholder" style="color:#dc2626">预览生成失败</div>';
  }
}

// ── LLM 生成预览 ──────────────────────────────────

async function llmPreviewMessage() {
  const offlineTime = $llmPreviewTime.value || "23:00";
  const advance = parseInt($llmPreviewAdvance.value) || 5;
  const floatRange = parseInt($llmPreviewFloat.value) || 0;

  $llmPreviewBtn.disabled = true;
  $llmPreviewBtn.textContent = "生成中...";
  $llmPreviewResult.innerHTML = '<div class="preview-placeholder">正在调用 LLM 生成通知...</div>';
  $llmPreviewMeta.style.display = "none";

  const data = await apiPost("/generate", {
    offline_time: offlineTime,
    countdown_minutes: advance,
  });

  $llmPreviewBtn.disabled = false;
  $llmPreviewBtn.textContent = "LLM 生成";

  // bridge 已剥壳：data 即 {text, source, avg_time_ms, error?}
  if (data && typeof data.text === "string") {
    const { text, source, avg_time_ms } = data;
    let floatInfo = "";
    if (floatRange > 0) {
      floatInfo = `\n<i>浮动范围: ±${floatRange} 分钟</i>`;
    }
    $llmPreviewResult.innerHTML = `<div class="preview-message">${escapeHtml(text)}${floatInfo}</div>`;

    // 显示元信息
    $llmPreviewMeta.style.display = "flex";
    if (source === "llm") {
      $llmSourceTag.textContent = "LLM 生成";
      $llmSourceTag.className = "source-tag llm";
    } else {
      $llmSourceTag.textContent = `模板回退 (${data.error || ""})`;
      $llmSourceTag.className = "source-tag template";
    }
    $llmTimeInfo.textContent = avg_time_ms ? `平均耗时 ${avg_time_ms}ms` : "";
    showToast(source === "llm" ? "LLM 生成成功" : "LLM 失败，已回退模板", source === "llm" ? "success" : "info");
  } else {
    $llmPreviewResult.innerHTML =
      '<div class="preview-placeholder" style="color:#dc2626">LLM 生成失败</div>';
    $llmPreviewMeta.style.display = "none";
    showToast("LLM 生成失败", "error");
  }
}

// ── 测试发送 ──────────────────────────────────────

async function testSend() {
  const groupId = $testGroupId.value.trim();
  if (!groupId) {
    $testResult.textContent = "请输入目标群号";
    $testResult.className = "test-result error";
    return;
  }

  $testSendBtn.disabled = true;
  $testSendBtn.textContent = "发送中...";

  const data = await apiPost("/test", { group_id: groupId });

  $testSendBtn.disabled = false;
  $testSendBtn.textContent = "发送测试通知";

  if (data && data.success) {
    $testResult.textContent = "测试通知已成功发送";
    $testResult.className = "test-result success";
    showToast("测试通知已发送", "success");
  } else {
    const errMsg = data?.error || "发送失败";
    $testResult.textContent = `发送失败: ${errMsg}`;
    $testResult.className = "test-result error";
    showToast(`发送失败: ${errMsg}`, "error");
  }
}

// ── 通知记录查询 ────────────────────────────────

async function refreshRecords() {
  $refreshRecordsBtn.disabled = true;
  $refreshRecordsBtn.textContent = "加载中...";

  const data = await apiGet("/records", { limit: 20 });

  $refreshRecordsBtn.disabled = false;
  $refreshRecordsBtn.textContent = "刷新记录";

  // bridge 已剥壳：data 即 {records, total, limit, offset}
  if (data && Array.isArray(data.records)) {
    renderRecords(data.records, data.total);
    showToast("记录已刷新", "success");
  }
}

function renderRecords(records, total) {
  $recordsCount.textContent = `共 ${total} 条记录`;

  if (!records || records.length === 0) {
    $recordsBody.innerHTML = '<tr><td colspan="7" class="empty-state">暂无通知发布记录</td></tr>';
    return;
  }

  $recordsBody.innerHTML = records
    .map((r) => {
      const dt = r.datetime || "未知";
      const name = escapeHtml(r.schedule_name || "未知");
      const offline = r.offline_time || "?";
      const actual = r.actual_trigger_minutes || "?";
      const floatS = r.float_seconds || 0;
      const source = r.message_source || "template";
      const results = r.results || {};
      const successCount = (results.success || []).length;
      const failedCount = (results.failed || []).length;

      const floatCell = floatS > 0
        ? `<span class="float-cell">${floatS}s</span>`
        : '<span style="color:#7f8c8d">精确</span>';

      const sourceCell = source === "llm"
        ? '<span class="source-llm">LLM</span>'
        : '<span class="source-template">模板</span>';

      let resultCell = `<span class="result-ok">成功 ${successCount}</span>`;
      if (failedCount > 0) {
        resultCell += ` / <span class="result-fail">失败 ${failedCount}</span>`;
      }

      return `<tr>
        <td>${dt}</td>
        <td>${name}</td>
        <td>${offline}</td>
        <td>${actual}min</td>
        <td>${floatCell}</td>
        <td>${sourceCell}</td>
        <td>${resultCell}</td>
      </tr>`;
    })
    .join("");
}

// ── 工具函数 ──────────────────────────────────────

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ── 事件绑定 ──────────────────────────────────────

$refreshStatusBtn.addEventListener("click", refreshStatus);
$previewBtn.addEventListener("click", previewMessage);
$llmPreviewBtn.addEventListener("click", llmPreviewMessage);
$testSendBtn.addEventListener("click", testSend);
$refreshRecordsBtn.addEventListener("click", refreshRecords);

// Schedule management
$refreshSchedulesBtn.addEventListener("click", refreshSchedules);
$addScheduleBtn.addEventListener("click", showAddForm);
$cancelScheduleBtn.addEventListener("click", hideForm);
$saveScheduleBtn.addEventListener("click", saveSchedule);

// 回车触发测试发送
$testGroupId.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    testSend();
  }
});

// ── 定时计划管理 ──────────────────────────────────

// 当前列表快照（供按索引操作）与表单模式（null=新增 / 字符串=编辑中的计划原名）
let currentSchedules = [];
let editingScheduleName = null;

async function refreshSchedules() {
  $refreshSchedulesBtn.disabled = true;
  $refreshSchedulesBtn.textContent = "加载中...";

  const data = await apiGet("/schedules");

  $refreshSchedulesBtn.disabled = false;
  $refreshSchedulesBtn.textContent = "刷新";

  // bridge 已剥壳：data 即 {schedules, total, day_type_labels}
  if (data && Array.isArray(data.schedules)) {
    renderSchedules(data.schedules, data.day_type_labels || {});
    showToast("计划列表已刷新", "success");
  }
}

function renderSchedules(schedules, labels) {
  // 缓存列表，供编辑/启停/删除按索引取用（避免把计划名拼进内联 onclick）
  currentSchedules = schedules || [];

  if (!schedules || schedules.length === 0) {
    $schedulesList.innerHTML = '<div class="empty-state">暂无定时计划，点击「+ 添加计划」创建</div>';
    return;
  }

  $schedulesList.innerHTML = schedules
    .map((s, idx) => {
      const name = escapeHtml(s.name || "?");
      const dayLabel = labels[s.day_type] || s.day_type || "每天";
      const time = s.offline_time || "?";
      const flt = s.float_range || 0;
      const enabled = s.enabled !== false;

      const floatText = flt > 0 ? ` 浮动±${flt}min` : "";
      const badgeClass = enabled ? "badge-on" : "badge-off";
      const badgeText = enabled ? "启用" : "禁用";
      const itemClass = enabled ? "" : "disabled";
      const toggleLabel = enabled ? "禁用" : "启用";

      return `
      <div class="schedule-item ${itemClass}" data-name="${name}">
        <div class="schedule-info">
          <div class="schedule-name-line">
            <span class="schedule-name">${name}</span>
            <span class="badge ${badgeClass}">${badgeText}</span>
          </div>
          <div class="schedule-detail">
            日期: ${dayLabel} | 下线: ${time}${floatText}
          </div>
        </div>
        <div class="schedule-actions">
          <button class="btn btn-sm" onclick="editSchedule(${idx})">编辑</button>
          <button class="btn btn-sm" onclick="toggleSchedule(${idx}, ${!enabled})">${toggleLabel}</button>
          <button class="btn btn-sm btn-danger" onclick="deleteSchedule(${idx})">删除</button>
        </div>
      </div>`;
    })
    .join("");
}

function showAddForm() {
  editingScheduleName = null;
  $scheduleFormTitle.textContent = "添加计划";
  $schedName.value = "";
  $schedName.disabled = false;
  $schedDayType.value = "everyday";
  $schedTime.value = "23:00";
  $schedFloat.value = "0";
  $scheduleForm.style.display = "block";
}

function showEditForm(schedule) {
  editingScheduleName = schedule.name;
  $scheduleFormTitle.textContent = `编辑计划「${schedule.name}」`;
  $schedName.value = schedule.name || "";
  // 名称是计划的唯一标识，编辑时不允许改名
  $schedName.disabled = true;
  $schedDayType.value = schedule.day_type || "everyday";
  $schedTime.value = schedule.offline_time || "23:00";
  $schedFloat.value = String(schedule.float_range || 0);
  $scheduleForm.style.display = "block";
}

function hideForm() {
  editingScheduleName = null;
  $schedName.disabled = false;
  $scheduleForm.style.display = "none";
}

async function saveSchedule() {
  const name = $schedName.value.trim();
  const dayType = $schedDayType.value;
  const offlineTime = $schedTime.value || "23:00";
  const floatRange = parseInt($schedFloat.value) || 0;
  const isEdit = !!editingScheduleName;

  if (!name) {
    showToast("请输入计划名称", "error");
    return;
  }

  $saveScheduleBtn.disabled = true;
  $saveScheduleBtn.textContent = "保存中...";

  const payload = {
    name: name,
    day_type: dayType,
    offline_time: offlineTime,
    float_range: Math.max(0, Math.min(floatRange, 10)),
  };
  // bridge SDK 无 PUT，修改走 POST + action=update
  if (isEdit) {
    payload.action = "update";
  }

  const data = await apiPost("/schedules", payload);

  $saveScheduleBtn.disabled = false;
  $saveScheduleBtn.textContent = "保存";

  if (data && data.success) {
    hideForm();
    refreshSchedules();
    refreshStatus(); // 同步刷新调度器状态
    showToast(
      data.message || (isEdit ? `已更新计划「${name}」` : `已添加计划「${name}」`),
      "success"
    );
  } else {
    showToast(data?.error || (isEdit ? "更新失败" : "添加失败"), "error");
  }
}

function editSchedule(idx) {
  const s = currentSchedules[idx];
  if (!s) {
    showToast("未找到该计划，请刷新后重试", "error");
    return;
  }
  showEditForm(s);
}

async function toggleSchedule(idx, enable) {
  const s = currentSchedules[idx];
  if (!s) {
    showToast("未找到该计划，请刷新后重试", "error");
    return;
  }

  const data = await apiPost("/schedules", {
    name: s.name,
    action: enable ? "enable" : "disable",
  });

  if (data && data.success) {
    refreshSchedules();
    refreshStatus();
    showToast(data.message, "success");
  } else {
    showToast(data?.error || "操作失败", "error");
  }
}

async function deleteSchedule(idx) {
  const s = currentSchedules[idx];
  if (!s) {
    showToast("未找到该计划，请刷新后重试", "error");
    return;
  }

  if (!confirm(`确定要删除计划「${s.name}」吗？此操作不可恢复。`)) {
    return;
  }

  const data = await apiPost("/schedules", { name: s.name, action: "delete" });

  if (data && data.success) {
    refreshSchedules();
    refreshStatus();
    showToast(data.message, "success");
  } else {
    showToast(data?.error || "删除失败", "error");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  refreshStatus();
  refreshRecords();
  refreshSchedules();
});