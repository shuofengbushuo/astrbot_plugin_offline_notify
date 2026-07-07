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

// Test
const $testGroupId = $("#testGroupId");
const $testSendBtn = $("#testSendBtn");
const $testResult = $("#testResult");

// Jobs
const $jobsList = $("#jobsList");

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

// ── 工具函数 ──────────────────────────────────────

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ── 事件绑定 ──────────────────────────────────────

$refreshStatusBtn.addEventListener("click", refreshStatus);
$previewBtn.addEventListener("click", previewMessage);
$testSendBtn.addEventListener("click", testSend);

// 回车触发测试发送
$testGroupId.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    testSend();
  }
});

// ── 初始化 ────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  refreshStatus();
});