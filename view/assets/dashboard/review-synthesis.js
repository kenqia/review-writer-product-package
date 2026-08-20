(function () {
  "use strict";
  const root = document.getElementById("synthesis-workspace-root");
  const projectSelect = document.getElementById("project");
  const projectSelection = window.reviewProjectSelection;
  if (!root || !projectSelect || !projectSelection) return;
  const text = (tag, value, className) => { const node = document.createElement(tag); if (className) node.className = className; node.textContent = value == null || value === "" ? "—" : String(value); return node; };
  const label = (value, fallback) => window.ReviewAuditUI.researcherLabel(value, fallback);
  const api = (id, suffix, options) => fetch(`/api/project/${encodeURIComponent(id)}/${suffix}`, options).then(response => { if (!response.ok) throw new Error(response.status === 409 ? "内容已更新，请刷新后重新核对。" : "综合判断暂不可用。"); return response.json(); });
  let busy = false;
  const coordinator = window.ReviewSessionUI.createProjectSurfaceCoordinator({
    getProjectId: () => projectSelection.getProjectId(projectSelect.value),
    getProjectLabel: () => projectSelection.getVisibleLabel(projectSelect.value),
    load: id => Promise.all([api(id,"comparison-protocol"), api(id,"synthesis"), api(id,"section-contracts"), api(id,"review-figures")]),
    render: values => render(...values),
    onProjectChange: () => showSynthesisState("正在读取当前项目的综合判断…", "workspace-empty"),
    onLoadError: error => showSynthesisState(error.message, "workspace-error"),
  });
  function showSynthesisState(value, className) { root.replaceChildren(text("p", value, className)); }
  function section(title) { const node = document.createElement("section"); node.className = "synthesis-panel"; node.append(text("h4", title)); return node; }
  function visibleList(value) { return Array.isArray(value) ? value.filter(item => typeof item === "string").join("、") : ""; }
  function describeAxis(value) {
    if (typeof value === "string") return label(value, "比较轴待核对");
    if (!value || typeof value !== "object") return "比较轴待核对";
    return [
      label(value.question || value.axis || value.name, "比较问题待核对"),
      `${(value.counterevidence_ids || value.counter_evidence || []).length} 条反证`,
      `${(value.incomparable_items || value.non_comparable || []).length} 项不可比内容`,
      visibleList(value.missing_units || value.missing_cells),
      label(value.impact_on_conclusion || value.impact, "对结论的影响待核对"),
    ].filter(Boolean).join(" · ") || "比较轴待核对";
  }
  function decisionLine(value) {
    const actor = {actor_type:value.actor_type, actor_label:value.actor_label};
    return `决定：${window.ReviewAuditUI.humanStatus(value.action)}；${window.ReviewAuditUI.decisionActor(actor)}；理由：${label(value.reason, "未提供")}`;
  }
  function describeFigurePlan(value) {
    const rows = Array.isArray(value) ? value : [];
    return rows.map(row => {
      if (typeof row === "string") return row;
      if (!row || typeof row !== "object") return "";
      return [
        label(row.figure_type || row.type, "图件任务"),
        label(row.scientific_question || row.rationale, "科学问题待提供"),
        `${(row.source_figure_ids || []).length} 张原论文图`,
        `${(row.placeholder_ids || []).length} 项专家制图任务`,
      ].filter(Boolean).join(" · ");
    }).filter(Boolean).join("；") || "尚未安排图位";
  }
  function targetOptionValue(option) { return `${option.marker}\u0000${option.occurrence}`; }
  function appendFigureTargetControls(parent, item, figures) {
    const manuscript = figures.manuscript || {};
    const sections = Array.isArray(manuscript.sections) ? manuscript.sections : [];
    const options = Array.isArray(item.target_options) ? item.target_options : [];
    const panel = document.createElement("div"); panel.className = "figure-target-binding";
    panel.append(text("strong", "修复图件归属"));
    panel.append(text("p", item.target_binding_status === "current" ? "当前归属已绑定；如正文变化需重新显式选择。" : "请选择当前 manuscript section 与已有 source/evidence marker；系统不会自动放置。", "evidence-meta"));
    const sectionLabel = document.createElement("label"); sectionLabel.textContent = "Manuscript section ";
    const sectionSelect = document.createElement("select"); sectionSelect.setAttribute("aria-label", "Manuscript section");
    const sectionPlaceholder = document.createElement("option"); sectionPlaceholder.value = ""; sectionPlaceholder.textContent = "请选择 section"; sectionSelect.append(sectionPlaceholder);
    sections.forEach(section => { const option = document.createElement("option"); option.value = section.section_id; option.textContent = section.heading ? `${section.heading} (${section.section_id})` : section.section_id; sectionSelect.append(option); });
    sectionLabel.append(sectionSelect);
    const markerLabel = document.createElement("label"); markerLabel.textContent = " Marker / occurrence ";
    const markerSelect = document.createElement("select"); markerSelect.setAttribute("aria-label", "Source or evidence marker"); markerLabel.append(markerSelect);
    const saveButton = document.createElement("button"); saveButton.type = "button"; saveButton.textContent = "保存图件归属";
    const message = text("p", "", "workspace-error");
    const current = item.target_binding_status === "current" && item.target_binding ? item.target_binding : null;
    function refreshMarkers() {
      markerSelect.replaceChildren();
      const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "请选择 marker"; markerSelect.append(placeholder);
      const eligible = options.filter(option => option.section_id === sectionSelect.value);
      eligible.forEach(option => { const entry = document.createElement("option"); entry.value = targetOptionValue(option); entry.textContent = `${option.marker} · occurrence ${option.occurrence}`; markerSelect.append(entry); });
      if (current && current.section_id === sectionSelect.value) markerSelect.value = targetOptionValue(current);
      saveButton.disabled = !sectionSelect.value || !markerSelect.value;
    }
    sectionSelect.addEventListener("change", refreshMarkers);
    markerSelect.addEventListener("change", () => { saveButton.disabled = !sectionSelect.value || !markerSelect.value; });
    if (current) sectionSelect.value = current.section_id;
    refreshMarkers();
    saveButton.addEventListener("click", () => {
      const selected = options.find(option => option.section_id === sectionSelect.value && targetOptionValue(option) === markerSelect.value);
      if (!selected) { message.textContent = "请显式选择有效的 section 与 marker。"; return; }
      decide("review-figures", {
        ...item,
        selection_status: "selected",
        target_binding: {
          figure_id: item.figure_id,
          asset_sha256: item.asset_sha256,
          manuscript_sha256: manuscript.sha256,
          section_id: selected.section_id,
          marker: selected.marker,
          occurrence: selected.occurrence,
        },
      });
    });
    panel.append(sectionLabel, markerLabel, saveButton, message);
    parent.append(panel);
  }
  function render(protocol, synthesis, contracts, figures) {
    root.replaceChildren();
    if (protocol.route !== "evidence-to-release.v1") {
      root.append(text("p", "综合判断将在 Paper Evidence 完成人工核对后显示。", "workspace-empty"));
      return;
    }
    const protocolPanel = section("Comparison Protocol");
    const p = protocol.protocol || {};
    const protocolNeedsReapproval = Boolean(p.decision)
      && (protocol.status === "needs_review" || protocol.status === "stale");
    protocolPanel.append(text("p", `比较对象：${(p.comparison_objects || []).length} 项已批准研究`));
    protocolPanel.append(text("p", `比较轴：${(p.axes || []).join("、") || "—"}`));
    protocolPanel.append(text("p", `归一化规则：${visibleList(p.normalization_rules) || "—"}`));
    protocolPanel.append(text("p", `缺失值规则：${p.missing_value_policy || "—"}`));
    protocolPanel.append(text("p", `不可比规则：${visibleList(p.incomparability_rules) || "—"}`));
    protocolPanel.append(text("p", `反证规则：${visibleList(p.counterevidence_rules) || "—"}`));
    protocolPanel.append(text("p", `结论强度：${p.claim_strength || "—"}`));
    if (p.decision) protocolPanel.append(text("p", decisionLine(p.decision), "decision-line"));
    if (!p.decision || protocolNeedsReapproval) {
      const protocolButton = document.createElement("button"); protocolButton.type = "button"; protocolButton.textContent = protocolNeedsReapproval ? "重新批准比较协议" : "批准比较协议"; protocolButton.disabled = !protocol.evidence_ready;
      if (!protocol.evidence_ready) protocolButton.title = "先完成 Paper Evidence 审查";
      protocolButton.addEventListener("click", () => decide("comparison-protocol", {version_token: p.version_token})); protocolPanel.append(protocolButton);
    }
    const coveragePanel = section("Coverage Map");
    const coverage = synthesis.coverage || {};
    coveragePanel.append(text("p", `研究范围：已批准研究集合；状态：${window.ReviewAuditUI.humanStatus(coverage.status)}`));
    const conflictLabels = {registered:"已登记冲突", checked_no_conflicts:"已核对，未发现冲突", not_checked:"尚未完成冲突核对"};
    coveragePanel.append(text("p", `冲突状态：${conflictLabels[coverage.conflict_status] || conflictLabels.not_checked}`));
    (coverage.conflict_register || []).forEach(item => coveragePanel.append(text("p", `冲突登记：${describeAxis(item)}`, "decision-line")));
    (coverage.axes || []).forEach(axis => coveragePanel.append(text("p", describeAxis(axis))));
    if (coverage.known_omissions?.length) coveragePanel.append(text("p", `已知遗漏：${coverage.known_omissions.join("、")}`));
    const claimPanel = section("Synthesis Claims");
    (synthesis.items || []).forEach(item => {
      const card = document.createElement("article"); card.className = "synthesis-card";
      card.append(text("strong", item.proposition), text("p", `比较轴：${item.comparison_axis}；边界：${item.applicability_boundary}`));
      card.append(text("p", `支持证据：${(item.supporting_evidence_ids || []).length} 条；反证：${(item.counter_evidence_ids || []).length} 条`));
      card.append(text("p", `不确定性：${item.uncertainty}；科学风险：${item.risk_class ? "已标记" : "未提供"}`, "evidence-meta"));
      if (item.decision) card.append(text("p", decisionLine(item.decision), "decision-line"));
      const button = document.createElement("button"); button.type = "button"; button.textContent = "记录决定"; button.disabled = !synthesis.protocol_ready; if (!synthesis.protocol_ready) button.title = "先批准 Comparison Protocol"; button.addEventListener("click", () => decide("synthesis", item)); card.append(button); claimPanel.append(card);
    });
    const contractPanel = section("Section Contracts");
    (contracts.items || []).forEach((item, index) => { const card = document.createElement("article"); card.className = "synthesis-card"; card.append(text("strong", label(item.heading, `第 ${index + 1} 节`)), text("p", item.research_question), text("p", `预期综合判断：${item.expected_synthesis}`), text("p", `图计划：${describeFigurePlan(item.figure_plan)}`)); if (item.decision) card.append(text("p", decisionLine(item.decision), "decision-line")); const button = document.createElement("button"); button.type = "button"; button.textContent = "记录决定"; button.disabled = !contracts.synthesis_ready; if (!contracts.synthesis_ready) button.title = "先完成 Synthesis Claims 审查"; button.addEventListener("click", () => decide("section-contracts", item)); card.append(button); contractPanel.append(card); });
    const figurePanel = section("原论文图片");
    (figures.locator_gaps || []).forEach(item => {
      const page = item.page ? `第 ${item.page} 页 · ` : "";
      figurePanel.append(text("p", `定位缺口：${page}${label(item.reason, "原论文图定位需要重建")}`, "workspace-error"));
    });
    (figures.source_figures || []).forEach(item => {
      const row = document.createElement("div"); row.className = "figure-source-row";
      const publication = item.publication_identity || {};
      const details = document.createElement("div");
      details.append(text("strong", label(publication.title, "论文身份未提供")));
      details.append(text("p", `作者：${visibleList(publication.authors) || "—"}；年份：${publication.year || "—"}；期刊：${publication.journal || "—"}；DOI：${publication.doi || "—"}`));
      details.append(text("p", `${item.figure_label} · 第 ${item.page} 页：${item.caption}`));
      details.append(text("p", `原始图块：${item.fragment_count || 1} 个；均来自同一论文页并按 content_list_v2 图注关系聚合。`, "evidence-meta"));
      details.append(text("p", item.attribution ? `出处：${item.attribution}` : "来源署名未提供"));
      details.append(text("p", item.rights_context?.status ? `复用权利状态已记录；${item.rights_context?.notice || "请按记录范围使用"}` : "复用权利状态未提供", "decision-line"));
      const previews = item.fragment_urls?.length ? item.fragment_urls : (item.image_url ? [item.image_url] : []);
      previews.forEach((url, index) => { const preview = document.createElement("img"); preview.src = url; preview.alt = `${item.caption || item.figure_label || "原论文图片"} · 图块 ${index + 1}`; preview.loading = "lazy"; preview.className = "source-figure-preview"; details.append(preview); });
      const links = document.createElement("div"); links.className = "figure-links";
      [[item.image_url, "新标签查看原图"], [item.pdf_page_url, "打开论文页"]].forEach(([href, label]) => { if (!href) return; const link = document.createElement("a"); link.href = href; link.target = "_blank"; link.rel = "noopener"; link.textContent = label; links.append(link); });
      details.append(links); appendFigureTargetControls(details, item, figures); row.append(details);
      const button = document.createElement("button"); button.type = "button"; button.textContent = item.selection_status === "selected" ? "取消选择" : "选择原图";
      button.addEventListener("click", () => decide("review-figures", {...item, selection_status: item.selection_status === "selected" ? "available" : "selected"})); row.append(button); figurePanel.append(row);
    });
    figurePanel.append(text("h4", "综合图制图任务", "placeholder-heading"));
    (figures.placeholders || []).forEach((item, index) => figurePanel.append(text("p", `任务：${label(item.scientific_question, `综合图任务 ${index + 1}`)}；读者结论：${label(item.reader_takeaway, "未提供")}；${label(item.gap_reason, "缺口原因未提供")}；状态：${window.ReviewAuditUI.humanStatus(item.status)}`, "figure-placeholder-row")));
    root.append(protocolPanel, coveragePanel, claimPanel, contractPanel, figurePanel);
  }
  async function decide(kind, item) {
    if (busy) return; busy = true; const reason = kind === "review-figures" ? "" : window.prompt("请记录这项决定的理由", item.decision?.reason || "研究者核对后决定");
    if (kind !== "review-figures" && !reason) { busy = false; return; }
    const body = kind === "comparison-protocol"
      ? {action:"approve", reason, version_token:item.version_token}
      : kind === "review-figures"
        ? {figure_id:item.figure_id, selection_status:item.selection_status, version_token:item.version_token, ...(item.target_binding ? {target_binding:item.target_binding} : {})}
        : {[kind === "synthesis" ? "synthesis_id" : "section_id"]: item[kind === "synthesis" ? "synthesis_id" : "section_id"], action:"approve", reason, version_token:item.version_token};
    if (kind !== "review-figures") Object.assign(body, window.reviewDecisionActor());
    try {
      await coordinator.mutate(
        id => api(id, kind, {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)}),
        {refreshAfterMutation: true, onError: error => { root.prepend(text("p", error.message, "workspace-error")); }},
      );
    } finally { busy = false; }
  }
  projectSelect.addEventListener("change", coordinator.projectChanged);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", coordinator.refresh);
  else coordinator.refresh();
}());
