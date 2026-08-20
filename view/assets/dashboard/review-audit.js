(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ReviewAuditUI = api;
}(typeof window !== "undefined" ? window : null, function () {
  "use strict";

  const object = value => value && typeof value === "object" ? value : {};
  const array = value => Array.isArray(value) ? value : [];
  const string = value => typeof value === "string" ? value.trim() : "";
  const finite = value => value !== null && value !== "" && Number.isFinite(Number(value));
  const opaquePattern = /^(?:scholarly|section|placeholder|marker|cmp|syn|pe)[-_:]|^(?:dashboard|local|simulated)-|^[a-z0-9]+(?:_[a-z0-9]+){1,}$|^[a-z]+-[0-9a-f]{8,}/i;
  const evaluationLabels = {
    WRONG_SOURCE_BINDING: "来源绑定与当前发布不一致",
    SUPPORTING_SOURCE_UNREAD: "支撑来源正文或补充信息尚未核读",
    HIGH_RISK_CLAIM_UNAPPROVED: "高风险科学主张尚未批准",
    STALE_APPROVAL: "科学批准已因上游变化失效",
    FABRICATED_SCIENTIFIC_DETAIL: "检出无证据支持的科学细节",
    STATE_SURFACE_DIVERGENCE: "磁盘、界面与发布状态不一致",
    UNSOURCED_SCIENTIFIC_CLAIM: "科学主张缺少来源绑定",
    LEGACY_DRAFT_REPACKAGED: "当前文稿仅为旧稿重新打包",
    SYSTEM_GENERATED_SYNTHESIS_FIGURE: "检出系统生成的综合科学图",
    SYNTHESIS_FIGURE_PENDING: "综合图仍待研究者完成",
  };

  function researcherLabel(value, fallback) {
    const candidate = string(value);
    return candidate && !opaquePattern.test(candidate) ? candidate : fallback;
  }

  function humanStatus(value) {
    return ({
      approved: "已闭合",
      needs_review: "需要核对",
      needs_attention: "需要处理",
      awaiting_human_figure: "等待专家制图",
      verified: "已验证",
      approve: "已批准",
      revise_and_approve: "修改后批准",
      reject: "已拒绝",
      pdf_locator_only: "仅原始 PDF 定位",
      reparse_required: "需要重新解析",
    })[string(value)] || "状态未提供";
  }

  function evaluationFindingLabel(value, fallback) {
    const candidate = string(value);
    return evaluationLabels[candidate] || researcherLabel(candidate, fallback);
  }

  function booleanLabel(value) {
    if (value === true) return "是";
    if (value === false) return "否";
    return "未提供";
  }

  function evaluationStateLabel(value) {
    return ({
      stale: "评估绑定已过期",
      invalid: "评估数据无效",
      unavailable: "评估数据不可用",
    })[string(value)] || "评估数据未提供";
  }

  function decisionActor(value) {
    const decision = object(value);
    const label = researcherLabel(decision.actor_label, "");
    if (label) return `决策者：${label}`;
    if (decision.actor_type === "human_researcher") return "决策者：研究者";
    if (decision.actor_type === "simulated_researcher_agent") return "决策者：研究者复核代理";
    return "决策者：未提供";
  }

  function protocolModel(payload) {
    const row = object(payload);
    const protocol = object(row.protocol);
    const decision = object(protocol.decision);
    const decisionParts = [
      `决定：${humanStatus(decision.action || row.status)}`,
      researcherLabel(decision.reason, ""),
      decisionActor(decision),
    ].filter(Boolean);
    return {
      title: "比较协议",
      status: humanStatus(row.status),
      studyScope: `${array(protocol.comparison_objects).length} 项已批准研究`,
      axes: array(protocol.axes).map((item, index) => researcherLabel(item, `比较轴 ${index + 1}`)),
      rules: [
        {label: "归一化规则", values: array(protocol.normalization_rules).map(item => researcherLabel(item, "规则内容未提供"))},
        {label: "缺失值规则", values: [researcherLabel(protocol.missing_value_policy, "规则内容未提供")]},
        {label: "不可比规则", values: array(protocol.incomparability_rules).map(item => researcherLabel(item, "规则内容未提供"))},
        {label: "反证规则", values: array(protocol.counterevidence_rules).map(item => researcherLabel(item, "规则内容未提供"))},
      ],
      claimStrength: researcherLabel(protocol.claim_strength, "结论强度未提供"),
      decision: decisionParts.join(" · "),
    };
  }

  function parseQualityModel(payload) {
    const row = object(payload);
    const summary = object(row.summary);
    const decisions = array(row.studies).flatMap(study => array(object(study).objects).map(item => object(item).decision));
    const actor = decisions.map(decisionActor).find(item => item !== "决策者：未提供") || "决策者：未提供";
    const updatedAt = string(row.last_decision_at);
    return {
      title: "解析质量",
      status: humanStatus(row.status),
      freshness: updatedAt ? `数据新鲜度：最近决定 ${updatedAt}` : "数据新鲜度：更新时间未提供",
      counts: `${finite(summary.studies) ? Number(summary.studies) : "—"} 篇研究 · ${finite(summary.objects) ? Number(summary.objects) : "—"} 个解析对象 · ${finite(summary.needs_review) ? Number(summary.needs_review) : "—"} 项待决定`,
      decision: `核对决定：${finite(summary.approved) ? Number(summary.approved) : "—"} 篇已批准；${finite(summary.pdf_locator_only) ? Number(summary.pdf_locator_only) : "—"} 项仅原始 PDF 定位；${finite(summary.reparse_required) ? Number(summary.reparse_required) : "—"} 项需要重新解析`,
      actor,
    };
  }

  function coverageModel(payload) {
    const coverage = object(object(payload).coverage);
    return {
      title: "综合判断覆盖",
      scope: "已批准研究集合",
      omissions: array(coverage.known_omissions).map(item => researcherLabel(item, "遗漏说明未提供")),
      axes: array(coverage.axes).map((value, index) => {
        const axis = object(value);
        return {
          title: `比较轴 ${index + 1}`,
          question: researcherLabel(axis.question, "科学问题未提供"),
          conflict: `${array(axis.counterevidence_ids).length} 条反证 · ${array(axis.incomparable_items).length} 项不可比内容`,
          impact: researcherLabel(axis.impact_on_conclusion, "对结论的影响未提供"),
        };
      }),
    };
  }

  function figureModel(payload) {
    const row = object(payload);
    const summary = object(row.summary);
    const sources = array(row.source_figures);
    const sourceCount = finite(summary.source_count) ? Number(summary.source_count) : sources.length;
    const missingRights = sources.some(item => {
      const source = object(item);
      return !string(source.attribution) || !string(object(source.rights_context).status);
    });
    return {
      title: "图件与披露",
      sourceSummary: `${sourceCount} 张原论文图${missingRights || !sources.length ? "；来源署名与复用权利未提供" : "；来源署名与复用权利已投影"}`,
      placeholders: array(row.placeholders).map((value, index) => {
        const item = object(value);
        return {
          title: researcherLabel(item.scientific_question, `综合图任务 ${index + 1}`),
          takeaway: researcherLabel(item.reader_takeaway, "读者要点未提供"),
          gap: researcherLabel(item.gap_reason, "缺口原因未提供"),
          status: humanStatus(item.status),
        };
      }),
    };
  }

  function evaluationModel(payload) {
    const finalPayload = object(payload);
    const report = object(finalPayload.quality_report);
    const evaluation = object(finalPayload.evaluation || finalPayload.benchmark || report.evaluation);
    const source = object(evaluation.benchmark || evaluation);
    const projectionStatus = string(source.status);
    const benchmarkStatus = string(
      source.benchmark_status || source.benchmarkStatus ||
      (projectionStatus && projectionStatus !== "available" ? projectionStatus : ""),
    );
    const stale = ["stale", "invalid", "unavailable"].includes(projectionStatus)
      || ["stale", "invalid", "unavailable"].includes(benchmarkStatus);
    const releaseBindingValue = source.release_binding || source.releaseBinding
      || evaluation.release_binding || evaluation.releaseBinding;
    const releaseBinding = releaseBindingValue && typeof releaseBindingValue === "object"
      && !Array.isArray(releaseBindingValue) ? releaseBindingValue : {};
    const releaseBindingDigest = string(
      releaseBinding.digest || releaseBinding.release_sha256
      || releaseBinding.manuscript_sha256 || releaseBinding.chemical_paper_binding_digest,
    );
    const rawScore = source.score !== undefined ? source.score : source.total;
    const dimensions = stale ? [] : array(source.rubric || source.dimensions || source.rationales).map((value, index) => {
      const row = object(value);
      return {
        name: researcherLabel(row.name || row.dimension || row.dimension_id, `评估维度 ${index + 1}`),
        score: finite(row.score) ? String(Number(row.score)) : "未提供",
        maxScore: finite(row.max_score) ? String(Number(row.max_score)) : "未提供",
        rationale: researcherLabel(row.rationale || row.reason, "理由未提供"),
      };
    });
    const hardFails = stale ? [] : array(source.hard_fails || source.hardFails).map(item => evaluationFindingLabel(item, "Hard Fail 内容未提供"));
    const issues = stale ? [] : array(source.issues).map(item => {
      const row = object(item);
      return evaluationFindingLabel(
        typeof item === "string" ? item : row.code || row.message,
        "问题内容未提供",
      );
    });
    const available = !stale && (finite(rawScore) || dimensions.length > 0 || hardFails.length > 0 || issues.length > 0);
    const disclaimer = string(source.disclaimer || evaluation.disclaimer || report.disclaimer);
    return {
      title: "发布评估",
      available,
      status: stale ? evaluationStateLabel(projectionStatus || benchmarkStatus) : available ? "评估数据已提供" : "评估数据未提供",
      benchmarkStatus: benchmarkStatus || "未提供",
      reasonCode: string(source.reason_code || source.reasonCode),
      stale,
      score: stale ? "未提供" : finite(rawScore) ? String(Number(rawScore)) : "未提供",
      tier: stale ? "未提供" : string(source.tier) || "未提供",
      dimensions,
      hardFailsTitle: "Hard Fails",
      hardFails,
      issuesTitle: "待处理问题",
      issues,
      expertReleaseReady: stale || typeof source.expert_release_ready !== "boolean"
        ? null : source.expert_release_ready,
      humanReviewRequired: stale || typeof source.human_review_required !== "boolean"
        ? null : source.human_review_required,
      disclaimer,
      releaseBinding,
      releaseBindingDigest: releaseBindingDigest || "未提供",
      releaseBoundary: "仅供内部 benchmark 评估，不构成发布批准或 B2 通过。",
    };
  }

  function buildAuditModel(input) {
    const value = object(input);
    return {
      parseQuality: parseQualityModel(value.parseQuality),
      protocol: protocolModel(value.protocol),
      coverage: coverageModel(value.synthesis),
      figures: figureModel(value.figures),
      evaluation: evaluationModel(value.final),
    };
  }

  function appendText(document, parent, tag, value, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = value;
    parent.append(node);
    return node;
  }

  function appendList(document, parent, rows, emptyText) {
    const list = document.createElement("ul");
    list.className = "audit-list";
    const values = rows.length ? rows : [emptyText];
    values.forEach(value => appendText(document, list, "li", value));
    parent.append(list);
  }

  function renderAudit(rootNode, input) {
    if (!rootNode || typeof document === "undefined") return;
    const model = buildAuditModel(input);
    rootNode.replaceChildren();

    const parse = document.createElement("section");
    parse.className = "audit-section";
    appendText(document, parse, "h3", model.parseQuality.title);
    appendText(document, parse, "strong", model.parseQuality.status, "audit-state");
    [model.parseQuality.counts, model.parseQuality.decision, model.parseQuality.actor, model.parseQuality.freshness]
      .forEach(value => appendText(document, parse, "p", value));

    const protocol = document.createElement("section");
    protocol.className = "audit-section audit-section-wide";
    appendText(document, protocol, "h3", model.protocol.title);
    appendText(document, protocol, "p", `${model.protocol.status} · ${model.protocol.studyScope} · ${model.protocol.decision}`);
    model.protocol.rules.forEach(rule => {
      appendText(document, protocol, "h4", rule.label);
      appendList(document, protocol, rule.values, "规则内容未提供");
    });
    appendText(document, protocol, "p", `结论强度：${model.protocol.claimStrength}`, "audit-emphasis");

    const evaluation = document.createElement("section");
    evaluation.className = "audit-section";
    appendText(document, evaluation, "h3", model.evaluation.title);
    appendText(document, evaluation, "strong", model.evaluation.status, "audit-state");
    if (model.evaluation.available) {
      appendText(document, evaluation, "p", `总分：${model.evaluation.score}`);
      appendText(document, evaluation, "p", `Tier：${model.evaluation.tier}`);
      appendText(document, evaluation, "p", `Benchmark 状态：${model.evaluation.benchmarkStatus}`);
      model.evaluation.dimensions.forEach(row => appendText(document, evaluation, "p", `${row.name} · ${row.score}/${row.maxScore} · ${row.rationale}`));
      appendText(document, evaluation, "h4", model.evaluation.hardFailsTitle);
      appendList(document, evaluation, model.evaluation.hardFails, "未报告 Hard Fails");
      appendText(document, evaluation, "h4", model.evaluation.issuesTitle);
      appendList(document, evaluation, model.evaluation.issues, "未报告待处理问题");
    } else {
      const staleMessage = model.evaluation.stale
        ? `${model.evaluation.status}：${model.evaluation.reasonCode || "未提供原因"}；已 fail-closed，未显示旧分数或 rubric。`
        : "当前冻结 API 未返回 benchmark 分数、七维理由、Hard Fails 或问题清单。";
      appendText(document, evaluation, "p", staleMessage);
    }
    appendText(document, evaluation, "p", `专家发布就绪（非发布批准）：${booleanLabel(model.evaluation.expertReleaseReady)}`);
    appendText(document, evaluation, "p", `需要人工复核：${booleanLabel(model.evaluation.humanReviewRequired)}`);
    appendText(document, evaluation, "p", `发布绑定摘要：${model.evaluation.releaseBindingDigest}`);
    if (model.evaluation.disclaimer) appendText(document, evaluation, "p", `免责声明：${model.evaluation.disclaimer}`);
    appendText(document, evaluation, "p", `发布边界：${model.evaluation.releaseBoundary}`, "audit-emphasis");

    const figures = document.createElement("section");
    figures.className = "audit-section";
    appendText(document, figures, "h3", model.figures.title);
    appendText(document, figures, "p", model.figures.sourceSummary);
    model.figures.placeholders.forEach(row => {
      appendText(document, figures, "h4", row.title);
      appendText(document, figures, "p", `${row.takeaway} · ${row.gap} · ${row.status}`);
    });

    const coverage = document.createElement("section");
    coverage.className = "audit-section";
    appendText(document, coverage, "h3", model.coverage.title);
    appendText(document, coverage, "p", model.coverage.scope);
    model.coverage.axes.forEach(row => appendText(document, coverage, "p", `${row.title} · ${row.question} · ${row.conflict} · ${row.impact}`));
    appendList(document, coverage, model.coverage.omissions, "已知遗漏未提供");

    rootNode.append(parse, evaluation, protocol, figures, coverage);
  }

  return {buildAuditModel, decisionActor, humanStatus, renderAudit, researcherLabel};
}));
