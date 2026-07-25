/**
 * AI下线通知系统 - WebUI 管理面板
 */

const API_BASE = "/astrbot_plugin_offline_notify";

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

async function apiGet(path) {
  try {
    const resp = await fetch(`${API_BASE}${path}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return await resp.json();
  } catch (err) {
    showToast(`请求失败: ${err.message}`, "error");
    return null;
  }
}

async function apiPost(path, body) {
  try {
    const resp = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return await resp.json();
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

function renderJobs(data) {
  if (!data.jobs || data.jobs.length === 0) {
    $jobsList.innerHTML = '<div class="empty-state">暂无定时任务</div>';
    return;
  }

  $jobsList.innerHTML = data.jobs
    .map(
      (job) => `
    <div class="job-item">
      <div class="job-info">
        <span class="job-name">${escapeHtml(job.name)}</span>
        <span class="job-next">下次触发: ${job.next_run || "暂无"}</span>
      </div>
      <span class="job-status active">活跃</span>
    </div>
  `
    )
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

  if (data && data.success) {
    const { title, body, footer } = data.data;
    let html = `<div class="preview-message"><strong>${escapeHtml(title)}</strong>\n\n${escapeHtml(body)}`;
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

  if (data && data.success && data.data) {
    const { text, source, avg_time_ms } = data.data;
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
      $llmSourceTag.textContent = `模板回退 (${data.data.error || ""})`;
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

  const data = await apiGet("/records?limit=20");

  $refreshRecordsBtn.disabled = false;
  $refreshRecordsBtn.textContent = "刷新记录";

  if (data && data.success && data.data) {
    renderRecords(data.data.records, data.data.total);
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

// 回车触发测试发送
$testGroupId.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    testSend();
  }
});

// ── 初始化 ────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  refreshStatus();
  refreshRecords();
});