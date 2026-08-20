(function () {
  "use strict";
  const root = document.getElementById("evidence-workspace-root");
  const shell = document.getElementById("evidence-synthesis-workspace");
  const status = document.getElementById("evidence-workspace-status");
  const message = document.getElementById("evidence-workspace-message");
  const projectSelect = document.getElementById("project");
  const projectSelection = window.reviewProjectSelection;
  if (!root || !shell || !projectSelect || !projectSelection) return;

  const text = (tag, value, className) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = value == null || value === "" ? "—" : String(value);
    return node;
  };
  const list = value => Array.isArray(value) && value.length ? value.join("、") : "—";
  const label = (value, fallback) => window.ReviewAuditUI.researcherLabel(value, fallback);
  const humanStatus = value => window.ReviewPresentation?.humanStatus?.(value)
    || window.ReviewAuditUI.humanStatus(value)
    || "待核对";
  const stateLabels = {
    SUPPORT: "支持",
    SUPPORTED: "支持",
    REFUTE: "反驳",
    REFUTED: "反驳",
    GAP: "证据缺口",
    CONFLICT: "存在冲突",
    PENDING: "待核对",
    NEEDS_REVIEW: "待核对",
    needs_review: "待核对",
    approved: "已核对",
    rejected: "已拒绝",
    stale: "来源已过期",
  };
  const epistemicLabels = {
    experimental_observation: "实验观察",
    author_interpretation: "作者解释",
    proposed_mechanism: "提出的机制",
  };
  const api = (id, suffix, options) => fetch(`/api/project/${encodeURIComponent(id)}/${suffix}`, options).then(response => {
    if (!response.ok) throw new Error(response.status === 409 ? "内容已更新，请刷新后重新核对。" : "工作台暂不可用。");
    return response.json();
  });
  let busy = false;

  const coordinator = window.ReviewSessionUI.createProjectSurfaceCoordinator({
    getProjectId: () => projectSelection.getProjectId(projectSelect.value),
    getProjectLabel: () => projectSelection.getVisibleLabel(projectSelect.value),
    load: id => api(id, "paper-evidence"),
    render,
    onProjectChange: () => showEvidenceState("正在读取当前项目的 Paper Evidence…", "workspace-empty"),
    onLoadError: error => showEvidenceState(error.message, "workspace-error"),
  });

  function showEvidenceState(value, className, statusText, messageText) {
    root.replaceChildren(text("p", value, className));
    shell.hidden = false;
    status.textContent = statusText || (className === "workspace-error" ? "Paper Evidence 暂不可用" : "正在读取 Paper Evidence");
    message.textContent = messageText || (className === "workspace-error" ? value : "切换项目后正在读取当前证据。");
  }

  function evidenceState(item) {
    const values = [item.verdict, item.status, item.reason_code, ...(item.risk_classes || [])]
      .filter(value => typeof value === "string");
    const matched = values.find(value => stateLabels[value] || stateLabels[value.toUpperCase()]);
    return matched ? (stateLabels[matched] || stateLabels[matched.toUpperCase()]) : humanStatus(item.status);
  }

  function currentness(item, payload) {
    if (typeof item.currentness === "string" && item.currentness.trim()) return item.currentness;
    if (item.status === "stale") return "来源已过期";
    const descriptorStatus = payload.source_pdf_descriptors?.status;
    if (descriptorStatus === "current") return "来源描述当前";
    if (descriptorStatus === "stale") return "来源描述已过期";
    return "当前性未提供";
  }

  function render(payload) {
    root.replaceChildren();
    if (payload.route !== "evidence-to-release.v1") {
      showEvidenceState(
        "尚未生成 Paper Evidence；来源准备完成后，研究者可在此逐项核对。",
        "workspace-empty",
        "等待来源与证据",
        "当前项目尚未进入 Evidence 阶段；不会显示内部处理面板。",
      );
      return;
    }
    shell.hidden = false;
    const legacyRisk = document.getElementById("risk-stage-panel");
    if (legacyRisk && payload.route === "evidence-to-release.v1") legacyRisk.hidden = true;
    if (shell.hidden) return;
    status.textContent = window.ReviewAuditUI.humanStatus(payload.status);
    message.textContent = "证据决定不会自动授权综合判断。";
    const items = payload.items || [];
    if (!items.length) { root.append(text("p", "尚未导入候选证据。", "workspace-empty")); return; }
    const listNode = document.createElement("div"); listNode.className = "evidence-card-list";
    items.forEach((item, index) => {
      const card = document.createElement("article"); card.className = "evidence-card";
      const heading = document.createElement("header");
      const statusNode = text("span", evidenceState(item), "workspace-status");
      statusNode.title = item.status || item.reason_code || "canonical status";
      heading.append(text("strong", item.statement), statusNode); card.append(heading);
      const studyLabel = label(item.study_label || item.citation || item.title, `研究 ${index + 1}`);
      card.append(text("p", `研究：${studyLabel} · 来源：${item.source_id || "未提供"}`, "evidence-meta"));
      card.append(text("p", `证据类型：${epistemicLabels[item.epistemic_type] || "证据"} · 定位：${item.locator?.section_or_item || item.locator?.figure_or_table || "未提供"} · 第 ${item.locator?.page || "?"} 页`, "evidence-meta"));
      card.append(text("p", `当前性：${currentness(item, payload)} · 状态：${evidenceState(item)}`, "evidence-meta"));
      card.append(text("p", `证据标记：${(item.risk_classes || []).map(value => stateLabels[value] || label(value, "研究风险")).join("、") || "未提供"}`));
      if (item.locator?.exact_quote) card.append(text("blockquote", item.locator.exact_quote));
      else card.append(text("p", "原文摘录：未提供", "evidence-meta"));
      if (item.decision) {
        const actor = {actor_type:item.decision.actor_type, actor_label:item.decision.actor_label};
        card.append(text("p", `${window.ReviewAuditUI.humanStatus(item.decision.action)} · ${window.ReviewAuditUI.decisionActor(actor)} · ${label(item.decision.reason, "理由未提供")}`, "decision-line"));
      }
      const references = document.createElement("p"); references.className = "evidence-links";
      [[item.pdf_page_url, "打开原论文页"], [item.parsed_text_url, "打开解析正文"]].forEach(([href, label]) => { if (!href) return; const link = document.createElement("a"); link.href = href; link.target = "_blank"; link.rel = "noopener"; link.textContent = label; references.append(link); });
      card.append(references);
      const actions = document.createElement("div"); actions.className = "workspace-actions";
      ["approve", "revise_and_approve", "reject"].forEach(action => {
        const button = document.createElement("button"); button.type = "button"; button.textContent = {approve:"批准", revise_and_approve:"修改后批准", reject:"拒绝"}[action];
        button.addEventListener("click", () => decide(item, action)); actions.append(button);
      }); card.append(actions); listNode.append(card);
    }); root.append(listNode);
  }

  async function decide(item, action) {
    if (busy) return; busy = true;
    const reason = window.prompt("请记录这项决定的理由", item.decision?.reason || "研究者核对后决定");
    if (!reason) { busy = false; return; }
    try {
      await coordinator.mutate(
        id => api(id, "paper-evidence", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({evidence_id:item.evidence_id, action, reason, version_token:item.version_token, ...window.reviewDecisionActor()})}),
        {renderResult: render, refreshAfterMutation: true, onError: error => { message.textContent = error.message; }},
      );
    } finally { busy = false; }
  }

  projectSelect.addEventListener("change", coordinator.projectChanged);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", coordinator.refresh);
  else coordinator.refresh();
}());
