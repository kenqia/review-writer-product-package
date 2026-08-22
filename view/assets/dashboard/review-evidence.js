(function () {
  "use strict";
  const root = document.getElementById("evidence-workspace-root");
  const shell = document.getElementById("evidence-synthesis-workspace");
  const status = document.getElementById("evidence-workspace-status");
  const message = document.getElementById("evidence-workspace-message");
  const decisionBundlePanel = document.getElementById("decision-bundle-panel");
  const decisionBundleRoot = document.getElementById("decision-bundle-root");
  const decisionBundleStatus = document.getElementById("decision-bundle-status");
  const decisionBundleMessage = document.getElementById("decision-bundle-message");
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
  const api = async (id, suffix, options) => {
    const response = await fetch(`/api/project/${encodeURIComponent(id)}/${suffix}`, options);
    if (!response.ok) {
      let details = null;
      try { details = await response.json(); } catch (_) { /* A plain HTTP error has no safe detail to show. */ }
      const detail = typeof details?.error === "string" ? details.error : details?.message;
      const error = new Error(response.status === 409
        ? "内容已更新，请刷新后重新核对。"
        : (typeof detail === "string" && detail.trim() ? detail.trim() : `决定未保存（${response.status}）。`));
      error.httpStatus = response.status;
      error.payload = details;
      throw error;
    }
    return response.json();
  };
  let busy = false;

  const decisionBundleStatusLabels = {
    HUMAN_ACTION_REQUIRED: "等待研究者决策",
    VERSION_CONFLICT: "当前版本冲突",
    PRECONDITION_FAILED: "前置条件未满足",
  };

  function decisionBundleFailure(error) {
    const payload = error?.payload;
    if (payload && typeof payload === "object" && typeof payload.status === "string") return payload;
    const reasonCode = error?.httpStatus === 409 ? "VERSION_CONFLICT" : "DECISION_BUNDLE_UNAVAILABLE";
    return {
      schema_version: "decision-bundle.v1",
      status: error?.httpStatus === 409 ? "VERSION_CONFLICT" : "PRECONDITION_FAILED",
      reason_code: reasonCode,
      category: error?.httpStatus === 409 ? "VERSION_CONFLICT" : "PRECONDITION_FAILED",
      current: null,
      revision: null,
      write_mode: "zero_write",
      current_unchanged: true,
      decision_options: [],
      expected_write_set: [],
      conflicts: [{component: "decision_bundle", code: reasonCode}],
    };
  }

  function normalizeDecisionBundle(payload) {
    if (!payload || typeof payload !== "object") return decisionBundleFailure();
    const statusValue = typeof payload.status === "string" ? payload.status : "";
    const current = payload.current && typeof payload.current === "object" ? payload.current : null;
    const isHumanAction = statusValue === "HUMAN_ACTION_REQUIRED";
    const currentRevision = current?.revision ?? payload.revision;
    if (isHumanAction && (
      payload.write_mode !== "NONE"
      || payload.current_unchanged !== true
      || !current
      || !Number.isInteger(currentRevision)
    )) {
      return decisionBundleFailure({payload:{status:"PRECONDITION_FAILED", reason_code:"DECISION_BUNDLE_INVALID"}});
    }
    return {
      ...payload,
      status: statusValue || "PRECONDITION_FAILED",
      current,
      decision_options: Array.isArray(payload.decision_options) ? payload.decision_options : [],
      expected_write_set: Array.isArray(payload.expected_write_set) ? payload.expected_write_set : [],
      conflicts: Array.isArray(payload.conflicts) ? payload.conflicts : [],
      write_mode: payload.write_mode || "zero_write",
      current_unchanged: payload.current_unchanged === true,
    };
  }

  function decisionBundleGaps(bundle) {
    const rows = [];
    const append = value => {
      if (!value || typeof value !== "object") return;
      const component = typeof value.component === "string" ? value.component : "decision_bundle";
      const code = typeof value.code === "string" ? value.code : "DECISION_BUNDLE_REVIEW_REQUIRED";
      const detail = typeof value.detail === "string" ? value.detail : "";
      const key = `${component}|${code}|${detail}`;
      if (rows.some(row => row.key === key)) return;
      rows.push({key, text: [component, code, detail].filter(Boolean).join(" · ")});
    };
    (bundle.conflicts || []).forEach(append);
    ["source_identity_projection", "parse_provenance", "evidence", "synthesis", "figures", "release_impacts"]
      .forEach(component => (bundle[component]?.gaps || []).forEach(gap => append({...gap, component:gap.component || component})));
    return rows;
  }

  function renderDecisionBundle(payload) {
    if (!decisionBundlePanel || !decisionBundleRoot || !decisionBundleStatus || !decisionBundleMessage) return;
    const bundle = normalizeDecisionBundle(payload);
    decisionBundlePanel.hidden = false;
    decisionBundleStatus.textContent = decisionBundleStatusLabels[bundle.status] || "状态待核对";
    decisionBundleStatus.className = `state-badge ${bundle.status === "HUMAN_ACTION_REQUIRED" ? "decision-bundle-status-warn" : "decision-bundle-status-error"}`;
    if (bundle.status === "HUMAN_ACTION_REQUIRED") {
      decisionBundleMessage.textContent = "HUMAN_ACTION_REQUIRED：等待研究者决策；当前面板只读，不会自动批准或写入。";
    } else if (bundle.status === "VERSION_CONFLICT") {
      decisionBundleMessage.textContent = "当前版本已变化；Decision Bundle 已停止显示候选决定，本次保持 zero-write，请刷新后重新读取。";
    } else {
      decisionBundleMessage.textContent = `Decision Bundle 暂不可用（${bundle.reason_code || "PRECONDITION_FAILED"}）；当前状态保持不变。`;
    }
    decisionBundleRoot.replaceChildren();
    const current = bundle.current || {};
    const summary = document.createElement("p");
    summary.className = "decision-bundle-summary";
    summary.textContent = `当前版本：${current.version_id || "—"} · revision：${current.revision ?? bundle.revision ?? "—"} · 状态：${bundle.status} · 写入模式：${bundle.write_mode || "zero_write"} · 当前未改变：${bundle.current_unchanged === true ? "是" : "否"}`;
    decisionBundleRoot.append(summary);

    const appendList = (heading, values, empty) => {
      const section = document.createElement("section");
      section.className = "decision-bundle-section";
      const title = document.createElement("h4");
      title.textContent = heading;
      section.append(title);
      const listNode = document.createElement("ul");
      if (values.length) {
        values.forEach(value => {
          const item = document.createElement("li");
          item.textContent = String(value);
          listNode.append(item);
        });
      } else {
        const item = document.createElement("li");
        item.textContent = empty;
        listNode.append(item);
      }
      section.append(listNode);
      decisionBundleRoot.append(section);
    };

    appendList(
      "候选决策（均需研究者确认）",
      bundle.status === "HUMAN_ACTION_REQUIRED"
        ? bundle.decision_options.map(option => option?.label || option?.decision_id || "候选决策")
        : [],
      bundle.status === "HUMAN_ACTION_REQUIRED" ? "未提供候选决策。" : "当前版本冲突或前置条件失败；不显示候选决策。",
    );
    appendList(
      "预期写集（只读说明）",
      bundle.status === "HUMAN_ACTION_REQUIRED" ? bundle.expected_write_set : [],
      bundle.status === "HUMAN_ACTION_REQUIRED" ? "未提供预期写集。" : "zero-write：本次没有可执行写集。",
    );
    appendList(
      "明确缺口",
      decisionBundleGaps(bundle).map(row => row.text),
      "当前投影未提供额外缺口；仍需研究者完成上述人工决策。",
    );
  }

  const coordinator = window.ReviewSessionUI.createProjectSurfaceCoordinator({
    getProjectId: () => projectSelection.getProjectId(projectSelect.value),
    getProjectLabel: () => projectSelection.getVisibleLabel(projectSelect.value),
    load: async id => {
      const [evidenceResult, decisionBundleResult] = await Promise.allSettled([
        api(id, "paper-evidence"),
        api(id, "decision-bundle"),
      ]);
      const decisionBundle = decisionBundleResult.status === "fulfilled"
        ? decisionBundleResult.value
        : decisionBundleFailure(decisionBundleResult.reason);
      if (evidenceResult.status === "rejected") {
        evidenceResult.reason.decisionBundle = decisionBundle;
        throw evidenceResult.reason;
      }
      const evidence = evidenceResult.value;
      return {evidence, decisionBundle};
    },
    render: payload => {
      const evidencePayload = payload?.evidence || payload;
      render(evidencePayload);
      renderDecisionBundle(payload?.decisionBundle || decisionBundleFailure());
    },
    onProjectChange: () => {
      showEvidenceState("正在读取当前项目的 Paper Evidence…", "workspace-empty");
      renderDecisionBundle({status:"PRECONDITION_FAILED", reason_code:"DECISION_BUNDLE_LOADING", write_mode:"zero_write", current_unchanged:true});
    },
    onLoadError: error => {
      showEvidenceState(error.message, "workspace-error");
      renderDecisionBundle(error?.decisionBundle || decisionBundleFailure(error));
    },
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
      const decisionForm = document.createElement("div"); decisionForm.className = "evidence-decision-form";
      const reasonLabel = text("label", "核对理由", "evidence-decision-label");
      const reason = document.createElement("textarea"); reason.name = "evidence-decision-reason"; reason.required = true; reason.rows = 3; reason.placeholder = "说明你为何批准、修改或拒绝这条证据。";
      reason.value = item.decision?.reason || ""; reasonLabel.htmlFor = `evidence-reason-${index}`; reason.id = reasonLabel.htmlFor;
      const replacementLabel = text("label", "修改后的证据表述", "evidence-decision-label");
      const replacement = document.createElement("textarea"); replacement.name = "evidence-replacement-statement"; replacement.required = false; replacement.rows = 3; replacement.placeholder = "仅在选择“修改后批准”时填写。";
      replacementLabel.htmlFor = `evidence-replacement-${index}`; replacement.id = replacementLabel.htmlFor; replacement.hidden = true; replacementLabel.hidden = true;
      const decisionStatus = text("p", "", "evidence-decision-status"); decisionStatus.setAttribute("role", "status"); decisionStatus.setAttribute("aria-live", "polite");
      decisionForm.append(reasonLabel, reason, replacementLabel, replacement, decisionStatus);
      const actions = document.createElement("div"); actions.className = "workspace-actions";
      const actionButtons = [];
      ["approve", "revise_and_approve", "reject"].forEach(action => {
        const button = document.createElement("button"); button.type = "button"; button.textContent = {approve:"批准", revise_and_approve:"修改后批准", reject:"拒绝"}[action];
        button.addEventListener("click", () => decide(item, action, {reason, replacement, replacementLabel, status:decisionStatus, buttons:actionButtons})); actionButtons.push(button); actions.append(button);
      }); card.append(decisionForm, actions); listNode.append(card);
    }); root.append(listNode);
  }

  function decisionError(controls, value) {
    controls.status.textContent = value;
    controls.status.className = "evidence-decision-status workspace-error";
  }

  function decisionBusy(controls, value) {
    controls.buttons.forEach(button => { button.disabled = value; });
  }

  async function decide(item, action, controls) {
    if (busy) return;
    controls.status.textContent = "";
    controls.status.className = "evidence-decision-status";
    if (action === "revise_and_approve") {
      controls.replacement.hidden = false;
      controls.replacementLabel.hidden = false;
      controls.replacement.required = true;
    }
    const reason = controls.reason.value.trim();
    if (!reason) {
      decisionError(controls, "请先填写核对理由；本次决定不会保存。");
      controls.reason.focus();
      return;
    }
    const replacement = controls.replacement.value.trim();
    if (action === "revise_and_approve" && !replacement) {
      decisionError(controls, "“修改后批准”需要填写修改后的证据表述；本次决定不会保存。");
      controls.replacement.focus();
      return;
    }
    busy = true; decisionBusy(controls, true);
    const body = {evidence_id:item.evidence_id, action, reason, version_token:item.version_token, ...window.reviewDecisionActor()};
    if (action === "revise_and_approve") body.replacement_statement = replacement;
    try {
      await coordinator.mutate(
        id => api(id, "paper-evidence", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)}),
        {renderResult: render, refreshAfterMutation: true, onError: error => { decisionError(controls, error.message); }},
      );
    } finally { busy = false; decisionBusy(controls, false); }
  }

  projectSelect.addEventListener("change", coordinator.projectChanged);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", coordinator.refresh);
  else coordinator.refresh();
}());
