(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.ReviewDualParseUI = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const studyTargetByModel = new WeakMap();
  const preflightTargetByModel = new WeakMap();
  const completionTargetByModel = new WeakMap();
  const reconciliationTargetByModel = new WeakMap();

  function object(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function array(value) {
    return Array.isArray(value) ? value : [];
  }

  function text(value, fallback) {
    return typeof value === "string" && value.trim() ? value.trim() : fallback;
  }

  function publicText(value, fallback) {
    const candidate = text(value, "");
    if (!candidate) return fallback;
    if (/(?:^|\s)(?:\/(?:home|mnt|users|tmp|private)\/|[a-z]:\\)/i.test(candidate)) return fallback;
    if (/(?:https?|file):\/\/|^\/\//i.test(candidate)) return fallback;
    if (/\b[a-f0-9]{64}\b/i.test(candidate)) return fallback;
    if (/(?:token|session|cookie)\s*[:=]/i.test(candidate)) return fallback;
    if (/^[\[{]/.test(candidate) || /\bV(?:2000|3000)\b|M\s+END/.test(candidate)) return fallback;
    return candidate;
  }

  function hasPlausibleSmilesSyntax(value) {
    const withoutBrackets = value.replace(/\[[^\]\r\n]{1,200}\]/g, "");
    if (/[\[\]]/.test(withoutBrackets)) return false;
    if (!/(?:Cl|Br|[BCNOPSFIbcnops]|\[[^\]\r\n]{1,200}\])/.test(value)) return false;
    return /^(?:(?:Cl|Br)|[BCNOPSFIbcnops]|[0-9@+\-()=#$%.:\/\\*])+$/.test(withoutBrackets);
  }

  function publicChemicalText(value) {
    const candidate = text(value, "");
    if (!candidate || candidate.length > 1000) return null;
    if (/(?:^|\s)(?:\/(?:home|mnt|users|tmp|private)\/|[a-z]:\\)/i.test(candidate)) return null;
    if (!hasPlausibleSmilesSyntax(candidate)) return null;
    if (/(?:token|session|cookie)\s*[:=]/i.test(candidate)) return null;
    if (/\bV(?:2000|3000)\b|M\s+END/.test(candidate)) return null;
    if (/^\s*\{/.test(candidate)) return null;
    return candidate;
  }

  function stateLabel(kind, status) {
    const labels = {
      pdf: {
        verified: "PDF 已核验",
        missing: "PDF 待补齐",
        stale: "PDF 核验已失效",
        failed: "PDF 核验失败",
        unknown: "PDF 状态未知",
      },
      generic: {
        current: "Generic Parse 当前有效",
        pending: "Generic Parse 正在处理",
        missing: "Generic Parse 待启动",
        stale: "Generic Parse 已过期",
        failed: "Generic Parse 失败",
        unknown: "Generic Parse 状态未知",
      },
      chemical: {
        current: "Chemical import 当前有效",
        imported: "Chemical import 当前有效",
        needs_review: "Chemical import 已导入，待研究者补全/复核",
        needs_import: "Chemical Paper 待确认导入",
        preflight_ready: "Chemical import 预检待确认",
        stale: "Chemical import 已过期",
        failed: "Chemical import 失败",
        unknown: "Chemical import 状态未知",
      },
      completion: {
        current: "Chemical Completion 已完成",
        complete: "Chemical Completion 已完成",
        needs_review: "Chemical Completion 待补全",
        blocked: "Chemical Completion 尚未开放",
        stale: "Chemical Completion 已过期",
        unknown: "Chemical Completion 状态未知",
      },
      reconciliation: {
        current: "Reconciliation 已闭合",
        complete: "Reconciliation 已闭合",
        needs_review: "Reconciliation 待核对",
        blocked: "Reconciliation 尚未开放",
        stale: "Reconciliation 已过期",
        unknown: "Reconciliation 状态未知",
      },
      evidence: {
        available: "Paper Evidence 可用",
        current: "Paper Evidence 可用",
        unavailable: "Paper Evidence 尚不可用",
        blocked: "Paper Evidence 尚不可用",
        stale: "Paper Evidence 已过期",
        unknown: "Paper Evidence 状态未知",
      },
    };
    return labels[kind]?.[status] || labels[kind]?.unknown || "状态未知";
  }

  function nonNegativeInteger(value) {
    return Number.isInteger(value) && value >= 0 ? value : null;
  }

  function positiveInteger(value) {
    return Number.isInteger(value) && value > 0 ? value : null;
  }

  function ratioValue(value) {
    return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1
      ? value : null;
  }

  function percentageLabel(value, fallback = "未知") {
    const ratio = ratioValue(value);
    return ratio === null ? fallback : `${Number((ratio * 100).toFixed(2))}%`;
  }

  function resolutionStatus(value) {
    return ["CONFIRMED", "AI_PROVISIONAL", "BLOCKED"].includes(value) ? value : "UNKNOWN";
  }

  function resolutionStatusLabel(value) {
    return ({
      CONFIRMED: "CONFIRMED",
      AI_PROVISIONAL: "AI_PROVISIONAL",
      BLOCKED: "BLOCKED",
      UNKNOWN: "解析状态未知",
    })[value] || "解析状态未知";
  }

  function provenanceModel(value) {
    const row = object(value);
    const locator = object(row.pdf_locator || row.pdfLocator);
    const page = positiveInteger(locator.page);
    const figureLabel = publicText(locator.figure_label || locator.figureLabel, "");
    const pdfLocator = page === null
      ? null
      : {page, ...(figureLabel ? {figureLabel} : {})};
    return {
      source: publicText(row.source, "来源未提供"),
      pdfLocator,
    };
  }

  function gapModel(value, index) {
    const row = object(value);
    const status = resolutionStatus(text(row.status || row.resolved_smiles_status, "BLOCKED"));
    const moleculeIndex = nonNegativeInteger(row.molecule_index);
    return {
      displayLabel: moleculeIndex === null ? `分子条目 ${index + 1}` : `分子条目 ${moleculeIndex + 1}`,
      status,
      statusLabel: resolutionStatusLabel(status),
      gapReason: publicText(row.gap_reason, "阻塞原因未提供"),
      value: status === "BLOCKED" ? null : publicChemicalText(row.value),
      actorProvenanceResidual: publicText(
        row.actor_provenance_residual,
        "保留 append-only actor provenance residual。",
      ),
    };
  }

  function honestProgressiveModel(value) {
    const row = object(value);
    const availability = ["available", "unknown", "unavailable"].includes(row.availability)
      ? row.availability : "unknown";
    const status = ["ready", "needs_more_traceable_candidates", "unknown", "unavailable"].includes(row.status)
      ? row.status : "unknown";
    return {
      availability,
      status,
      availabilityReason: publicText(row.availability_reason, "待 Chemical Paper 导入；状态未知。"),
      coreMoleculeCount: nonNegativeInteger(row.core_molecule_count),
      coverageDenominator: nonNegativeInteger(row.coverage_denominator),
      confirmedCount: nonNegativeInteger(row.confirmed_count),
      aiProvisionalCount: nonNegativeInteger(row.ai_provisional_count),
      blockedCount: nonNegativeInteger(row.blocked_count),
      coverageRatio: ratioValue(row.coverage_ratio),
      coverageThreshold: ratioValue(row.coverage_threshold),
      workflowCanContinue: typeof row.workflow_can_continue === "boolean" ? row.workflow_can_continue : null,
      uncertaintyStatement: publicText(row.uncertainty_statement, "不确定性说明未提供。"),
      gapRegistry: array(row.gap_registry).map(gapModel),
      actorProvenanceResidual: publicText(
        row.actor_provenance_residual,
        "保留 append-only actor provenance residual。",
      ),
    };
  }

  function inputCoverageLaneModel(value) {
    const row = object(value);
    const status = ["current", "needs_review", "missing", "unknown"].includes(row.status)
      ? row.status : "unknown";
    return {
      available: nonNegativeInteger(row.available),
      total: positiveInteger(row.total),
      status,
      statusLabel: ({
        current: "当前有效",
        needs_review: "待核验",
        missing: "待补齐",
        unknown: "未知",
      })[status],
    };
  }

  function inputCoverageModel(value) {
    const row = object(value);
    const lanes = object(row.lanes);
    const laneModels = {
      mainPdf: inputCoverageLaneModel(lanes.main_pdf),
      si: inputCoverageLaneModel(lanes.si),
      chemicalZip: inputCoverageLaneModel(lanes.chemical_zip),
      genericParse: inputCoverageLaneModel(lanes.generic_parse),
    };
    const studies = array(row.studies).map(value => {
      const study = object(value);
      return {
        studyId: text(study.study_id, ""),
        siStatus: ["current", "needs_review", "missing", "unknown"].includes(study.si_status)
          ? study.si_status : "unknown",
        chemicalZipStatus: ["current", "needs_review", "missing", "unknown"].includes(study.chemical_zip_status)
          ? study.chemical_zip_status : "unknown",
      };
    });
    const hardGate = publicText(row.hard_gate, "未知/未知/未知/未知");
    const hardGateLabel = publicText(
      row.hard_gate_label,
      `主 PDF ${laneModels.mainPdf.available ?? "未知"}/${laneModels.mainPdf.total ?? "未知"} · `
        + `SI ${laneModels.si.available ?? "未知"}/${laneModels.si.total ?? "未知"} · `
        + `Chemical ZIP ${laneModels.chemicalZip.available ?? "未知"}/${laneModels.chemicalZip.total ?? "未知"} · `
        + `Generic Parse ${laneModels.genericParse.available ?? "未知"}/${laneModels.genericParse.total ?? "未知"}`,
    );
    return {
      contractValid: row.schema_version === "dashboard-input-coverage.v1",
      hardGate,
      hardGateLabel,
      ready: typeof row.ready === "boolean" ? row.ready : null,
      sourceDisclosure: publicText(
        row.source_disclosure,
        "当前输入仅披露来源可用性与 currentness；原始 PDF 是科学仲裁来源。",
      ),
      lanes: laneModels,
      studies,
    };
  }

  function safePdfUrl(value) {
    const candidate = text(value, "");
    if (!candidate.startsWith("/api/project/") || candidate.startsWith("//")) return "";
    if (/(?:token|session|cookie)=/i.test(candidate)) return "";
    return candidate;
  }

  function normalizedBbox(value) {
    const bbox = array(value);
    if (
      bbox.length !== 4
      || !bbox.every(Number.isFinite)
      || bbox.some(coordinate => coordinate < 0 || coordinate > 1)
      || bbox[0] >= bbox[2]
      || bbox[1] >= bbox[3]
    ) return null;
    return bbox.slice();
  }

  function percentValue(value) {
    return String(Number((value * 100).toFixed(4)));
  }

  function regionLabel(bbox) {
    return `区域 x ${percentValue(bbox[0])}–${percentValue(bbox[2])}% · y ${percentValue(bbox[1])}–${percentValue(bbox[3])}%`;
  }

  function locatorModel(row, showRegion) {
    const provenanceLocator = object(object(row).provenance).pdf_locator;
    const page = positiveInteger(row.page) ?? positiveInteger(object(provenanceLocator).page);
    const bbox = normalizedBbox(row.bbox_normalized);
    const model = {
      locatorLabel: page
        ? `第 ${page} 页 · ${bbox ? (showRegion ? regionLabel(bbox) : "页面区域已定位") : "页面区域未提供"}`
        : "PDF 定位未提供",
      pdfPageUrl: safePdfUrl(row.pdf_page_url),
      page,
    };
    if (showRegion && bbox) model.normalizedBbox = bbox;
    return model;
  }

  function studyModel(value, index) {
    const row = object(value);
    const pdfStatus = text(row.pdf_status, "unknown");
    const rawGenericStatus = text(row.generic_parse_status, "unknown");
    const genericStatus = ["current", "pending", "missing", "stale", "failed"].includes(rawGenericStatus)
      ? rawGenericStatus : "unknown";
    const rawChemicalStatus = text(row.chemical_import_status, "unknown");
    const chemicalStatus = rawChemicalStatus === "missing" ? "needs_import" : rawChemicalStatus;
    const chemicalFacts = [];
    if (chemicalStatus === "needs_review") {
      const pageCount = positiveInteger(row.page_count);
      const moleculeCount = nonNegativeInteger(row.molecule_count);
      const engine = [publicText(row.backend, ""), publicText(row.version, "")].filter(Boolean).join(" · ");
      const importedAt = publicText(row.imported_at, "");
      if (pageCount !== null) chemicalFacts.push(`${pageCount} 页`);
      if (moleculeCount !== null) chemicalFacts.push(`${moleculeCount} 个分子条目`);
      if (engine) chemicalFacts.push(engine);
      if (row.reaction_data_status === "unavailable_not_provided") {
        chemicalFacts.push("反应数据：导出包未提供");
      }
      if (importedAt) chemicalFacts.push(`导入时间：${importedAt}`);
    }
    const completionStatus = text(row.completion_status, "unknown");
    const unresolvedReconciliation = Number.isInteger(row.unresolved_reconciliation_count)
      && row.unresolved_reconciliation_count > 0;
    const reconciliationStatus = text(
      object(row.reconciliation).status,
      unresolvedReconciliation ? "needs_review" : text(row.reconciliation_status, "unknown"),
    );
    const evidenceStatus = text(row.paper_evidence_status, "unknown");
    const missingNameCount = nonNegativeInteger(row.missing_name_count);
    const missingResolvedSmilesCount = nonNegativeInteger(row.missing_resolved_smiles_count);
    const aiAuthoredSmilesCount = nonNegativeInteger(row.ai_authored_smiles_count);
    const completionGapLabel = missingNameCount !== null && missingResolvedSmilesCount !== null
      ? `缺失名称 ${missingNameCount} · 缺失已解析 SMILES ${missingResolvedSmilesCount}`
      : missingNameCount === null && missingResolvedSmilesCount === null
        ? "补全缺口数未提供"
        : [
            missingNameCount === null ? "缺失名称数未提供" : `缺失名称 ${missingNameCount}`,
            missingResolvedSmilesCount === null
              ? "缺失已解析 SMILES 数未提供"
              : `缺失已解析 SMILES ${missingResolvedSmilesCount}`,
          ].join(" · ");
    const aiAuthoredSmilesLabel = aiAuthoredSmilesCount === null
      ? "AI 生成 SMILES 数未提供"
      : `AI 生成 SMILES ${aiAuthoredSmilesCount}`;
    if (chemicalStatus === "needs_review") {
      chemicalFacts.push(completionGapLabel, aiAuthoredSmilesLabel);
    } else {
      if (missingResolvedSmilesCount !== null) {
        chemicalFacts.push(`缺失已解析 SMILES ${missingResolvedSmilesCount}`);
      }
      if (aiAuthoredSmilesCount !== null) chemicalFacts.push(aiAuthoredSmilesLabel);
    }
    const model = {
      displayLabel: `研究 ${index + 1}`,
      citation: publicText(row.citation, `Core study ${index + 1}`),
      tierLabel: (row.tier || row.source_tier) === "background" ? "Background" : (row.tier || row.source_tier) === "core" ? "Core" : "分层未知",
      confirmedCount: nonNegativeInteger(row.confirmed_count),
      aiProvisionalCount: nonNegativeInteger(row.ai_provisional_count),
      blockedCount: nonNegativeInteger(row.blocked_count),
      coverageRatio: ratioValue(row.coverage_ratio),
      coverageDenominator: nonNegativeInteger(
        row.coverage_denominator ?? row.molecule_count,
      ),
      coverageThreshold: ratioValue(row.coverage_threshold),
      uncertaintyStatement: publicText(row.uncertainty_statement, "不确定性说明未提供。"),
      actorProvenanceResidual: publicText(
        row.actor_provenance_residual,
        "保留 append-only actor provenance residual。",
      ),
      pdfLabel: stateLabel("pdf", pdfStatus),
      genericStatus,
      genericLabel: stateLabel("generic", genericStatus),
      chemicalStatus,
      chemicalLabel: stateLabel("chemical", chemicalStatus),
      chemicalFacts,
      completionLabel: stateLabel("completion", completionStatus),
      reconciliationLabel: stateLabel("reconciliation", reconciliationStatus),
      evidenceLabel: stateLabel("evidence", evidenceStatus),
      missingNameCount,
      missingResolvedSmilesCount,
      completionGapLabel,
      aiAuthoredSmilesCount,
      aiAuthoredSmilesLabel,
      actorLabel: publicText(row.actor_label, "决定者未提供"),
      updatedLabel: publicText(row.updated_at, "更新时间未提供"),
    };
    const studyId = text(row.study_id, "");
    if (studyId) {
      studyTargetByModel.set(model, {studyId});
      Object.defineProperty(model, "_studyId", {value: studyId, enumerable: false});
    }
    return model;
  }

  function smilesCandidatesModel(value) {
    const row = object(value);
    const sourceLabels = {
      smiles_expanded: "展开候选",
      smiles_unexpanded: "未展开候选",
      researcher_correction: "研究者更正",
    };
    return {
      expanded: publicChemicalText(row.expanded),
      unexpanded: publicChemicalText(row.unexpanded),
      selectedSource: sourceLabels[row.selected_source] || "流程来源未选择",
      difference: row.candidate_difference === true,
    };
  }

  function completionCandidateSuggestionsModel(value) {
    return array(value).map(candidateValue => {
      const candidate = object(candidateValue);
      const locator = object(candidate.pdf_locator || candidate.pdfLocator);
      const page = positiveInteger(locator.page);
      const rawProvenance = object(candidate.provenance);
      const provenance = {};
      Object.entries(rawProvenance).forEach(([key, item]) => {
        // The authoritative writer accepts only flat scalar provenance.  The
        // locator remains a separate field; never carry a nested locator into
        // the write payload.
        if (key === "pdf_locator" || key === "pdfLocator") return;
        if (!/^[A-Za-z][A-Za-z0-9_]{0,99}$/.test(key)) return;
        if (typeof item === "string") {
          const safe = publicText(item, "");
          if (safe) provenance[key] = safe;
        } else if (item === null || typeof item === "boolean") {
          provenance[key] = item;
        } else if (typeof item === "number" && Number.isFinite(item)) {
          provenance[key] = item;
        }
      });
      const valueText = publicChemicalText(candidate.value);
      const reason = publicText(candidate.reason, "候选理由未提供");
      if (!valueText || page === null) return null;
      return {
        value: valueText,
        confidence: ratioValue(candidate.confidence),
        provenance,
        provenanceLabel: publicText(provenance.source || provenance.kind, "候选来源未提供"),
        page,
        figureLabel: publicText(locator.figure_label || locator.figureLabel, ""),
        reason,
      };
    }).filter(Boolean);
  }

  function importPreflightModel(value) {
    const row = object(value);
    if (!Object.keys(row).length) return null;
    const pageCount = positiveInteger(row.page_count);
    const moleculeCount = nonNegativeInteger(row.molecule_count);
    const status = text(row.status, "unknown");
    const fileKindLabels = {
      layout: "版面数据",
      markdown: "Markdown",
      molecule_info: "分子信息",
    };
    const model = {
      status,
      statusLabel: ({
        ready_for_confirmation: "预检完成，等待确认导入",
        checking: "正在检查 Chemical Paper ZIP",
        failed: "Chemical Paper ZIP 预检失败",
        stale: "预检结果已过期",
      })[status] || "尚无 Chemical Paper ZIP 预检",
      confirmAvailable: status === "ready_for_confirmation",
      pageLabel: pageCount === null ? "页数未提供" : `${pageCount} 页`,
      moleculeLabel: moleculeCount === null ? "分子条目数未提供" : `${moleculeCount} 个分子条目`,
      engineLabel: [publicText(row.backend, ""), publicText(row.version, "")].filter(Boolean).join(" · ") || "解析引擎与版本未提供",
      fileKindsLabel: array(row.file_kinds).map(kind => fileKindLabels[kind]).filter(Boolean).join("、") || "文件种类未提供",
      gaps: array(row.gaps).map(value => publicText(value, "")).filter(Boolean),
      actorLabel: publicText(row.actor_label, "决定者未提供"),
      updatedLabel: publicText(row.updated_at, "更新时间未提供"),
    };
    const studyId = text(row.study_id, "");
    const preflightToken = text(row.preflight_token, "");
    if (studyId && preflightToken) preflightTargetByModel.set(model, {studyId, preflightToken});
    if (!preflightTargetByModel.has(model)) model.confirmAvailable = false;
    return model;
  }

  function completionModel(value) {
    const row = object(value);
    const fieldLabels = {
      mol_idt: "名称或论文局部标签",
      resolved_smiles: "已解析 SMILES",
    };
    if (!fieldLabels[row.field]) return null;
    const field = row.field;
    const moleculeIndex = nonNegativeInteger(row.molecule_index);
    const status = resolutionStatus(text(row.resolved_smiles_status, "UNKNOWN"));
    const provenance = provenanceModel(row.provenance);
    const blocked = status === "BLOCKED";
    const model = {
      displayLabel: moleculeIndex === null ? "分子条目序号未提供" : `分子条目 ${moleculeIndex + 1}`,
      field,
      fieldLabel: fieldLabels[field] || "未知化学字段",
      resolvedSmilesStatus: status,
      resolvedSmilesStatusLabel: resolutionStatusLabel(status),
      confidence: ratioValue(row.confidence),
      provenance,
      provenanceSource: provenance.source,
      provenanceLocator: provenance.pdfLocator,
      gapReason: blocked ? publicText(row.gap_reason, "阻塞原因未提供") : null,
      actorProvenanceResidual: publicText(
        row.actor_provenance_residual,
        "保留 append-only actor provenance residual。",
      ),
      ...locatorModel(row, true),
      actorLabel: publicText(row.actor_label, "决定者未提供"),
      updatedLabel: publicText(row.updated_at, "更新时间未提供"),
      candidateSuggestions: completionCandidateSuggestionsModel(row.candidate_suggestions),
    };
    if (field === "resolved_smiles") {
      model.resolvedSmiles = blocked ? null : publicChemicalText(row.resolved_smiles);
      model.smilesCandidates = smilesCandidatesModel(row.smiles_candidates);
    }
    const studyId = text(row.study_id, "");
    const versionToken = text(row.version_token, "");
    if (studyId && moleculeIndex !== null && versionToken) {
      completionTargetByModel.set(model, {studyId, moleculeIndex, versionToken});
    }
    return model;
  }

  function reconciliationModel(value) {
    const row = object(value);
    const decision = object(row.decision);
    const selectedLane = ["generic", "chemical"].includes(decision.selected_lane)
      ? decision.selected_lane : null;
    const status = text(row.status, "unknown");
    const model = {
      kindLabel: ({
        text: "正文",
        table: "表格",
        figure: "图",
        formula: "公式",
        molecule: "分子",
      })[row.kind] || "解析对象",
      status,
      statusLabel: ({
        corroborated: "两层候选相互印证",
        complementary: "两层候选互补",
        conflict: "两层候选冲突",
        single_lane_only: "仅单层可定位",
        needs_review: "等待 PDF 核对",
        stale: "核对决定已过期",
        blocked: "当前对象已阻塞",
        pdf_resolved: "已由 PDF 仲裁",
      })[status] || "核对状态未知",
      genericCandidate: publicText(row.generic_candidate, "Generic 候选未提供"),
      chemicalCandidate: publicText(row.chemical_candidate, "Chemical 候选未提供"),
      selectedLane,
      allowedActions: ["pdf_resolved", "pdf_locator_only", "reject_both"],
      ...locatorModel(row),
      actorLabel: publicText(decision.actor_label, publicText(row.actor_label, "决定者未提供")),
      updatedLabel: publicText(decision.recorded_at, publicText(row.updated_at, "更新时间未提供")),
    };
    const studyId = text(row.study_id, "");
    const objectId = text(row.object_id, "");
    const registryDigest = text(row.registry_digest, "");
    if (studyId && objectId && registryDigest) {
      reconciliationTargetByModel.set(model, {studyId, objectId, registryDigest});
    }
    return model;
  }

  function emptyModel() {
    return {
      contractValid: false,
      route: "unknown",
      status: "unknown",
      statusLabel: "双层解析安全投影尚不可用",
      failureMessage: "",
      retryable: false,
      nextAction: {
        label: "等待双层解析状态",
        description: "Evidence 保持锁定。",
      },
      studies: [],
      importPreflight: null,
      completionQueue: [],
      reconciliationItems: [],
      honestProgressive: honestProgressiveModel({}),
      inputCoverage: inputCoverageModel({}),
      summary: {
        coreStudies: null,
        genericCurrent: null,
      },
    };
  }

  function projectionModel(input) {
    const value = object(input);
    if (value.schema_version !== "dual-parse-projection.v2") return emptyModel();
    const nextAction = object(value.next_action);
    const summary = object(value.summary);
    const status = ["loading", "ready", "failed", "stale", "unavailable"].includes(value.status)
      ? value.status : "unknown";
    const honestRoute = value.route === "honest_progressive";
    const honestProgressive = honestRoute ? honestProgressiveModel(value.honest_progressive) : null;
    const inputCoverage = inputCoverageModel(value.input_coverage);
    const projectedStudies = array(value.studies).map(studyModel);
    const currentnessByStudy = new Map(inputCoverage.studies.map(study => [study.studyId, study]));
    const projectedWithInputCurrentness = inputCoverage.contractValid
      ? projectedStudies.map(study => {
        const currentness = currentnessByStudy.get(study._studyId);
        if (!currentness) return study;
        const next = {
          ...study,
          siStatus: currentness.siStatus,
          siLabel: `SI ${stateLabel("generic", currentness.siStatus)}`,
          chemicalZipStatus: currentness.chemicalZipStatus,
          chemicalZipLabel: `Chemical ZIP ${stateLabel("chemical", currentness.chemicalZipStatus)}`,
        };
        const target = studyTargetByModel.get(study);
        if (target) studyTargetByModel.set(next, target);
        return next;
      })
      : projectedStudies;
    const studies = !honestRoute || honestProgressive.availability === "available"
      ? projectedWithInputCurrentness
      : projectedWithInputCurrentness.map(study => {
        // Keep the import affordance visible even before the first Chemical
        // archive makes the Honest Progressive availability lane known.
        // Counts and completion status below remain explicitly unknown.
        const next = {
          ...study,
          chemicalLabel: study.chemicalLabel === stateLabel("chemical", "needs_import")
            ? study.chemicalLabel
            : "Chemical Paper 待导入；状态未知",
          chemicalFacts: ["待 Chemical Paper 导入；状态未知"],
          completionLabel: "Chemical Completion 状态未知",
          confirmedCount: null,
          aiProvisionalCount: null,
          blockedCount: null,
          coverageRatio: null,
          coverageThreshold: honestProgressive.coverageThreshold,
          uncertaintyStatement: honestProgressive.uncertaintyStatement,
        };
        const target = studyTargetByModel.get(study);
        if (target) studyTargetByModel.set(next, target);
        return next;
      });
    return {
      ...emptyModel(),
      contractValid: true,
      route: value.route === "honest_progressive" ? "honest_progressive" : "unknown",
      status,
      statusLabel: ({
        loading: "正在读取双层解析任务",
        ready: "双层解析状态已读取",
        failed: "双层解析任务失败",
        stale: "双层解析状态需要刷新",
        unavailable: "双层解析尚未就绪",
      })[status] || "双层解析状态未知",
      failureMessage: publicText(value.failure_message, ""),
      retryable: value.retryable === true,
      nextAction: {
        label: publicText(nextAction.label, "等待当前阻塞项明确"),
        description: publicText(nextAction.description, "Evidence 保持锁定。"),
      },
      studies,
      importPreflight: importPreflightModel(value.import_preflight),
      completionQueue: array(value.completion_queue).map(completionModel).filter(Boolean),
      reconciliationItems: array(value.reconciliation_items).map(reconciliationModel),
      honestProgressive: honestProgressive || honestProgressiveModel({}),
      inputCoverage: {
        ...inputCoverage,
        studies: inputCoverage.studies.map(({studyId, ...study}) => study),
      },
      summary: {
        coreStudies: nonNegativeInteger(summary.core_studies),
        genericCurrent: nonNegativeInteger(summary.generic_current),
      },
    };
  }

  function availabilityModel(input) {
    const value = object(input);
    const includedStudies = nonNegativeInteger(value.includedStudies);
    const sourceRows = array(object(value.sources).sources)
      .filter(row => text(object(row).role, "").toUpperCase() === "MAIN");
    const sourceStatusByStudy = new Map();
    let sourceRowsValid = sourceRows.length > 0;
    sourceRows.forEach(value => {
      const row = object(value);
      const studyId = text(row.study_id, "");
      const status = text(row.status, "");
      if (!studyId || !["已获得", "需要上传"].includes(status)) {
        sourceRowsValid = false;
        return;
      }
      if (!sourceStatusByStudy.has(studyId)) sourceStatusByStudy.set(studyId, new Set());
      sourceStatusByStudy.get(studyId).add(status);
    });
    const sourceTotal = includedStudies !== null
      ? includedStudies : sourceStatusByStudy.size || null;
    const sourceCoverageKnown = sourceRowsValid
      && (includedStudies === null || sourceStatusByStudy.size <= includedStudies);
    const dualParse = object(value.dualParse).contractValid === true
      ? value.dualParse : projectionModel(value.dualParse);
    const coreStudies = nonNegativeInteger(object(dualParse.summary).coreStudies);
    const genericCurrent = nonNegativeInteger(object(dualParse.summary).genericCurrent);
    const projectedCoreRows = array(dualParse.studies)
      .filter(row => object(row).tierLabel === "Core");
    const projectedCoreStudies = projectedCoreRows.length;
    const projectedGenericCurrent = projectedCoreRows
      .filter(row => object(row).genericStatus === "current").length;
    const genericRowsKnown = projectedCoreRows.every(row =>
      object(row).genericStatus !== "unknown"
    );
    const coreWithinIncluded = includedStudies !== null
      && coreStudies !== null && coreStudies <= includedStudies;
    const genericKnown = dualParse.status === "ready"
      && coreStudies !== null && genericCurrent !== null
      && genericCurrent <= coreStudies
      && coreStudies === projectedCoreStudies
      && genericCurrent === projectedGenericCurrent
      && genericRowsKnown
      && coreWithinIncluded
      && (coreStudies > 0 || includedStudies === 0);
    const reviewedEvidence = nonNegativeInteger(value.reviewedEvidenceStudies);
    const reviewedEvidenceKnown = reviewedEvidence !== null
      && includedStudies !== null && reviewedEvidence <= includedStudies;
    return {
      mainFullText: {
        available: sourceCoverageKnown
          ? Array.from(sourceStatusByStudy.values()).filter(statuses => statuses.has("已获得")).length
          : null,
        total: sourceTotal,
      },
      genericSource: {
        available: genericKnown ? genericCurrent : null,
        total: genericKnown ? coreStudies : includedStudies,
      },
      reviewedEvidence: {
        available: reviewedEvidenceKnown ? reviewedEvidence : null,
        total: includedStudies,
      },
    };
  }

  function required(value, label) {
    const normalized = text(value, "");
    if (!normalized) throw new Error(`${label} required`);
    return normalized;
  }

  function actorPayload(input) {
    const actor = object(input);
    const actorType = required(actor.actorType, "actor type");
    if (!["human_researcher", "simulated_researcher_agent"].includes(actorType)) {
      throw new Error("researcher actor required");
    }
    return {
      actor_type: actorType,
      actor_label: required(actor.actorLabel, "actor label"),
    };
  }

  function importPreflightRequest(studyId, file) {
    if (!file || typeof file !== "object") throw new Error("ZIP file required");
    return {
      study_id: required(studyId, "study id"),
      file,
    };
  }

  function importConfirmRequest(studyId, preflightToken, actor) {
    return {
      study_id: required(studyId, "study id"),
      preflight_token: required(preflightToken, "preflight token"),
      ...actorPayload(actor || {actorType: "human_researcher", actorLabel: "研究者"}),
    };
  }

  function pdfLocatorPayload(input) {
    const locator = object(input);
    if (!Number.isInteger(locator.page) || locator.page < 1) throw new Error("PDF page required");
    const result = {page: locator.page};
    const figureLabel = publicText(locator.figureLabel || locator.figure_label, "");
    if (figureLabel) result.figure_label = figureLabel;
    return result;
  }

  function requiredPublic(value, label) {
    return required(publicText(value, ""), label);
  }

  function confidencePayload(value) {
    const confidence = ratioValue(value);
    if (confidence === null) throw new Error("confidence required");
    return confidence;
  }

  function provenancePayload(input, pdfLocator) {
    const value = object(input);
    const result = {};
    Object.entries(value).forEach(([key, item]) => {
      if (key === "pdf_locator" || key === "pdfLocator") return;
      if (!/^[A-Za-z][A-Za-z0-9_]{0,99}$/.test(key)) return;
      if (typeof item === "string") {
        result[key] = requiredPublic(item, `provenance ${key}`);
      } else if (item === null || typeof item === "boolean") {
        result[key] = item;
      } else if (typeof item === "number" && Number.isFinite(item)) {
        result[key] = item;
      }
    });
    result.source = requiredPublic(result.source || result.kind, "provenance source");
    // Keep the PDF locator at the correction boundary.  Nested objects are
    // rejected by the authoritative chemical-paper provenance contract.
    pdfLocatorPayload(value.pdfLocator || value.pdf_locator || pdfLocator);
    return result;
  }

  function completionBatchRequest(studyId, versionToken, rows, actor) {
    const allowedFields = new Set(["mol_idt", "resolved_smiles"]);
    const actorFields = actorPayload(actor);
    const corrections = array(rows).map(rowValue => {
      const row = object(rowValue);
      if (!Number.isInteger(row.moleculeIndex) || row.moleculeIndex < 0) throw new Error("molecule index required");
      const field = required(row.field, "field");
      if (!allowedFields.has(field)) throw new Error("unsupported field");
      const pdfLocator = pdfLocatorPayload(row.pdfLocator);
      const requestedStatus = text(row.resolutionStatus, "");
      const status = requestedStatus ? resolutionStatus(requestedStatus) : "";
      if (requestedStatus && status === "UNKNOWN") throw new Error("unsupported resolution status");
      if (status === "BLOCKED") throw new Error("blocked candidate cannot be submitted");
      if (status === "CONFIRMED" && actorFields.actor_type === "simulated_researcher_agent") {
        throw new Error("AI actor cannot confirm candidate");
      }
      const value = field === "resolved_smiles"
        ? requiredPublic(publicChemicalText(row.value), "value")
        : requiredPublic(row.value, "value");
      const correction = {
        molecule_index: row.moleculeIndex,
        field,
        value,
        reason: requiredPublic(row.reason, "reason"),
        pdf_locator: pdfLocator,
      };
      if (status === "AI_PROVISIONAL") {
        correction.resolution_status = status;
        correction.confidence = confidencePayload(row.confidence);
        correction.provenance = provenancePayload(row.provenance, row.pdfLocator);
      } else if (status === "CONFIRMED") {
        correction.resolution_status = status;
      }
      return correction;
    });
    if (!corrections.length) throw new Error("corrections required");
    return {
      study_id: required(studyId, "study id"),
      version_token: required(versionToken, "version token"),
      ...actorFields,
      corrections,
    };
  }

  function reconciliationRequest(studyId, objectId, registryDigest, decision, actor) {
    const value = object(decision);
    const action = required(value.action, "action");
    if (!["pdf_resolved", "pdf_locator_only", "reject_both"].includes(action)) {
      throw new Error("unsupported reconciliation action");
    }
    const result = {
      study_id: required(studyId, "study id"),
      object_id: required(objectId, "object id"),
      registry_digest: required(registryDigest, "registry version"),
      action,
    };
    const selectedLane = text(value.selectedLane, "");
    if (selectedLane && !["generic", "chemical"].includes(selectedLane)) throw new Error("unsupported lane");
    if (action === "pdf_resolved" && !selectedLane) throw new Error("selected lane required");
    if (action !== "pdf_resolved" && selectedLane) throw new Error("selected lane not applicable");
    if (selectedLane) result.selected_lane = selectedLane;
    result.note = required(value.note, "note");
    result.pdf_locator = pdfLocatorPayload(value.pdfLocator);
    Object.assign(result, actorPayload(actor));
    return result;
  }

  function appendText(document, parent, tag, value, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = value;
    parent.append(node);
    return node;
  }

  function labelledControl(document, labelText, control) {
    const label = document.createElement("label");
    label.append(document.createTextNode(labelText), control);
    return label;
  }

  function safeActor(handlers) {
    const actor = object(handlers?.actor);
    return {
      actorType: text(actor.actorType, "human_researcher"),
      actorLabel: text(actor.actorLabel, "研究者"),
    };
  }

  function openConfirmationDialog(document, returnFocus, title, description, confirmLabel, onConfirm) {
    const dialog = document.createElement("dialog");
    dialog.className = "dual-parse-dialog";
    dialog.setAttribute("aria-modal", "true");
    appendText(document, dialog, "h4", title);
    appendText(document, dialog, "p", description);
    const actions = document.createElement("div");
    actions.className = "dual-parse-dialog-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "返回核对";
    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.className = "dual-parse-primary";
    confirm.textContent = confirmLabel;
    actions.append(cancel, confirm);
    dialog.append(actions);
    document.body.append(dialog);

    function closeDialog() {
      if (typeof dialog.close === "function") dialog.close();
      else {
        dialog.removeAttribute("open");
        dialog.remove();
        returnFocus.focus();
      }
    }

    dialog.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeDialog();
        return;
      }
      if (event.key === "Tab") {
        const focusable = Array.from(dialog.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])"))
          .filter(node => !node.disabled && !node.hidden);
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    });
    dialog.addEventListener("close", () => {
      dialog.remove();
      returnFocus.focus();
    });
    cancel.addEventListener("click", closeDialog);
    confirm.addEventListener("click", () => {
      onConfirm?.();
      closeDialog();
    });
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    cancel.focus();
  }

  function countLabel(value) {
    return value === null ? "未知" : String(value);
  }

  function renderHonestProgressive(document, parent, model) {
    parent.replaceChildren();
    const honest = model.honestProgressive || honestProgressiveModel({});
    const header = document.createElement("header");
    appendText(document, header, "p", "Honest Progressive Route", "honest-progressive-kicker");
    appendText(document, header, "h4", "诚实渐进式结构解析");
    appendText(
      document,
      header,
      "p",
      "三态结果保持可见：CONFIRMED、AI_PROVISIONAL 与 BLOCKED 不互相伪装。",
      "honest-progressive-lead",
    );
    const statusLabel = honest.status === "needs_more_traceable_candidates"
      ? "状态：需要更多可追溯候选（流程可继续，缺口保持可见）"
      : honest.status === "ready"
        ? "状态：覆盖率已达到当前阈值"
        : honest.status === "unknown"
          ? "状态：未知"
          : "状态：不可用";
    appendText(document, header, "p", statusLabel, "honest-progressive-status");
    parent.append(header);

    const counts = document.createElement("div");
    counts.className = "honest-progressive-counts";
    [
      ["总分子", honest.coreMoleculeCount],
      ["CONFIRMED", honest.confirmedCount],
      ["AI_PROVISIONAL", honest.aiProvisionalCount],
      ["BLOCKED", honest.blockedCount],
    ].forEach(([label, value]) => {
      const card = document.createElement("article");
      appendText(document, card, "span", label);
      appendText(document, card, "strong", countLabel(value));
      counts.append(card);
    });
    parent.append(counts);

    const coverage = document.createElement("section");
    coverage.className = "honest-progressive-coverage";
    appendText(
      document,
      coverage,
      "strong",
      `覆盖率 ${percentageLabel(honest.coverageRatio)} · 阈值 ${percentageLabel(honest.coverageThreshold)}`,
    );
    parent.append(coverage);

    const uncertainty = document.createElement("section");
    uncertainty.className = "honest-progressive-uncertainty";
    appendText(document, uncertainty, "strong", "不确定性说明");
    appendText(document, uncertainty, "p", honest.uncertaintyStatement);
    parent.append(uncertainty);

    const gaps = document.createElement("section");
    gaps.className = "honest-progressive-gaps";
    appendText(document, gaps, "strong", "Gap registry");
    if (!honest.gapRegistry.length) {
      appendText(document, gaps, "p", "当前没有已登记的 BLOCKED gap。", "dual-parse-empty");
    } else {
      honest.gapRegistry.forEach(gap => {
        const item = document.createElement("article");
        appendText(document, item, "strong", `${gap.displayLabel} · ${gap.statusLabel}`);
        appendText(document, item, "p", gap.gapReason);
        if (gap.status === "BLOCKED") appendText(document, item, "p", "value=null");
        gaps.append(item);
      });
    }
    parent.append(gaps);
    appendText(
      document,
      parent,
      "p",
      `Actor provenance residual：${honest.actorProvenanceResidual}`,
      "honest-progressive-residual",
    );
  }

  function renderInputCoverage(document, parent, coverage) {
    const input = coverage || inputCoverageModel({});
    const section = document.createElement("section");
    section.className = "dual-parse-input-coverage";
    appendText(document, section, "h4", "输入硬门");
    appendText(document, section, "strong", input.hardGate);
    appendText(document, section, "p", input.sourceDisclosure);
    const lanes = document.createElement("ul");
    [
      ["主 PDF", input.lanes.mainPdf],
      ["SI", input.lanes.si],
      ["Chemical ZIP", input.lanes.chemicalZip],
      ["Generic Parse", input.lanes.genericParse],
    ].forEach(([label, lane]) => {
      appendText(document, lanes, "li", `${label} ${lane.available ?? "未知"}/${lane.total ?? "未知"} · ${lane.statusLabel}`);
    });
    section.append(lanes);
    input.studies.forEach((study, index) => {
      appendText(
        document,
        section,
        "p",
        `研究 ${index + 1} · SI ${studyStatusLabel(study.siStatus)} · Chemical ZIP ${studyStatusLabel(study.chemicalZipStatus)}`,
      );
    });
    parent.append(section);
  }

  function studyStatusLabel(status) {
    return ({
      current: "当前有效",
      needs_review: "待核验",
      missing: "待补齐",
      unknown: "状态未知",
    })[status] || "状态未知";
  }

  function renderStatus(document, parent, model, handlers) {
    parent.replaceChildren();
    const status = document.createElement("section");
    status.className = `dual-parse-live-state ${model.status}`;
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    appendText(document, status, "strong", model.statusLabel);
    if (model.failureMessage) appendText(document, status, "p", model.failureMessage, "dual-parse-failure");
    if (model.retryable) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.textContent = "重试当前任务";
      retry.addEventListener("click", () => handlers?.onRetry?.());
      status.append(retry);
    }
    const nextAction = document.createElement("section");
    nextAction.className = "dual-parse-next-action";
    nextAction.setAttribute("aria-label", "唯一下一步");
    appendText(document, nextAction, "span", "唯一下一步");
    appendText(document, nextAction, "strong", model.nextAction.label);
    appendText(document, nextAction, "p", model.nextAction.description);
    parent.append(status, nextAction);
  }

  function appendImportControl(document, card, study, handlers) {
    const target = studyTargetByModel.get(study);
    if (!target || study.chemicalStatus !== "needs_import") return;
    const form = document.createElement("form");
    form.className = "dual-parse-import-form";
    const file = document.createElement("input");
    file.type = "file";
    file.accept = ".zip,application/zip";
    const submit = document.createElement("button");
    submit.type = "submit";
    submit.textContent = "预检 Chemical Paper ZIP";
    form.append(labelledControl(document, "选择完整导出 ZIP", file), submit);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      try {
        const result = await handlers?.onImportPreflight?.(
          importPreflightRequest(target.studyId, file.files?.[0]),
          form,
        );
        if (result) handlers?.onPreflightResult?.(result);
      } catch (_) {
        handlers?.onValidationError?.("请选择需要预检的完整 Chemical Paper ZIP。");
      }
    });
    card.append(form);
  }

  function renderStudies(document, parent, model, handlers) {
    const list = document.createElement("div");
    list.className = "dual-study-grid";
    model.studies.forEach(study => {
      const card = document.createElement("article");
      card.className = "dual-study-card";
      const header = document.createElement("header");
      appendText(document, header, "span", study.displayLabel, "dual-study-number");
      appendText(document, header, "h4", study.citation);
      appendText(document, header, "strong", study.tierLabel);
      card.append(header);
      const states = document.createElement("ol");
      states.className = "dual-study-state-list";
      [
        study.pdfLabel,
        study.genericLabel,
        study.chemicalLabel,
        study.completionLabel,
        study.reconciliationLabel,
        study.evidenceLabel,
      ].forEach(value => appendText(document, states, "li", value));
      card.append(states);
      if (model.route === "honest_progressive") {
        const honestMetrics = document.createElement("section");
        honestMetrics.className = "dual-study-honest-metrics";
        appendText(
          document,
          honestMetrics,
          "strong",
          `论文覆盖率 ${percentageLabel(study.coverageRatio)} · ${countLabel(
            study.confirmedCount !== null && study.aiProvisionalCount !== null
              ? study.confirmedCount + study.aiProvisionalCount
              : null,
          )}/${countLabel(study.coverageDenominator)} · 阈值 ${percentageLabel(study.coverageThreshold)}`,
        );
        appendText(
          document,
          honestMetrics,
          "p",
          `CONFIRMED ${countLabel(study.confirmedCount)} · AI_PROVISIONAL ${countLabel(study.aiProvisionalCount)} · BLOCKED ${countLabel(study.blockedCount)}`,
        );
        appendText(document, honestMetrics, "p", `不确定性说明：${study.uncertaintyStatement}`);
        if (study.actorProvenanceResidual) {
          appendText(document, honestMetrics, "p", `Actor provenance residual：${study.actorProvenanceResidual}`);
        }
        card.append(honestMetrics);
      }
      if (study.chemicalFacts.length) {
        const facts = document.createElement("ul");
        facts.className = "dual-parse-facts";
        study.chemicalFacts.forEach(value => appendText(document, facts, "li", value));
        card.append(facts);
      }
      appendText(document, card, "p", `${study.actorLabel} · ${study.updatedLabel}`, "dual-parse-freshness");
      appendImportControl(document, card, study, handlers);
      list.append(card);
    });
    if (!model.studies.length) appendText(document, list, "p", "尚无可显示的双层解析研究状态。", "dual-parse-empty");
    parent.append(list);
  }

  function renderPreflight(document, parent, preflight, handlers) {
    parent.replaceChildren();
    appendText(document, parent, "h4", "Chemical import · 预检后确认");
    if (!preflight) {
      appendText(document, parent, "p", "选择 ZIP 只做预检；确认前不会写入权威状态。", "dual-parse-empty");
      return;
    }
    appendText(document, parent, "strong", preflight.statusLabel);
    const facts = document.createElement("ul");
    facts.className = "dual-parse-facts";
    [preflight.pageLabel, preflight.moleculeLabel, preflight.engineLabel, preflight.fileKindsLabel]
      .forEach(value => appendText(document, facts, "li", value));
    parent.append(facts);
    preflight.gaps.forEach(value => appendText(document, parent, "p", value, "dual-parse-gap"));
    appendText(document, parent, "p", `${preflight.actorLabel} · ${preflight.updatedLabel}`, "dual-parse-freshness");
    if (!preflight.confirmAvailable) return;
    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.className = "dual-parse-primary";
    confirm.textContent = "确认导入";
    confirm.addEventListener("click", () => {
      const target = preflightTargetByModel.get(preflight);
      if (!target) return;
      openConfirmationDialog(
        document,
        confirm,
        "确认 Chemical Paper 导入",
        "确认后将重新核对当前 PDF 绑定并写入权威状态。",
        "确认导入",
        () => handlers?.onImportConfirm?.(
          importConfirmRequest(target.studyId, target.preflightToken, safeActor(handlers)),
        ),
      );
    });
    parent.append(confirm);
  }

  function groupCompletionRows(rows) {
    const groups = new Map();
    rows.forEach(row => {
      const target = completionTargetByModel.get(row);
      if (!target) return;
      const key = `${target.studyId}\u0000${target.versionToken}`;
      if (!groups.has(key)) groups.set(key, {target, rows: []});
      groups.get(key).rows.push(row);
    });
    return Array.from(groups.values());
  }

  function drawCompletionCrop(image, canvas, bbox) {
    if (!image.naturalWidth || !image.naturalHeight) return false;
    const sourceLeft = bbox[0] * image.naturalWidth;
    const sourceTop = bbox[1] * image.naturalHeight;
    const sourceWidth = (bbox[2] * image.naturalWidth) - sourceLeft;
    const sourceHeight = (bbox[3] * image.naturalHeight) - sourceTop;
    if (sourceWidth <= 0 || sourceHeight <= 0) return false;
    const scale = Math.min(480 / sourceWidth, 320 / sourceHeight);
    canvas.width = Math.max(1, Math.round(sourceWidth * scale));
    canvas.height = Math.max(1, Math.round(sourceHeight * scale));
    const context = canvas.getContext("2d");
    if (!context) return false;
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = "high";
    context.drawImage(
      image,
      sourceLeft,
      sourceTop,
      sourceWidth,
      sourceHeight,
      0,
      0,
      canvas.width,
      canvas.height,
    );
    return true;
  }

  function completionLocator(document, row) {
    const locator = document.createElement("figure");
    locator.className = "dual-completion-locator";
    locator.setAttribute("aria-label", `${row.displayLabel} · ${row.locatorLabel} · 上下文定位`);
    const preview = document.createElement("span");
    preview.className = "dual-completion-page-preview";
    const image = document.createElement("img");
    image.className = "dual-completion-page-image";
    image.src = row.pdfPageUrl;
    image.alt = "";
    image.loading = "lazy";
    image.decoding = "async";
    const overlay = document.createElement("span");
    overlay.className = "dual-completion-bbox";
    overlay.setAttribute("aria-hidden", "true");
    overlay.style.left = `${percentValue(row.normalizedBbox[0])}%`;
    overlay.style.top = `${percentValue(row.normalizedBbox[1])}%`;
    overlay.style.width = `${percentValue(row.normalizedBbox[2] - row.normalizedBbox[0])}%`;
    overlay.style.height = `${percentValue(row.normalizedBbox[3] - row.normalizedBbox[1])}%`;
    preview.append(image, overlay);
    const caption = document.createElement("figcaption");
    caption.className = "dual-completion-locator-caption";
    caption.textContent = `红框为当前结构区域 · ${row.locatorLabel}`;

    const crop = document.createElement("details");
    crop.className = "dual-completion-crop";
    const summary = document.createElement("summary");
    summary.textContent = "查看当前结构区域局部放大";
    const canvas = document.createElement("canvas");
    canvas.className = "dual-completion-crop-canvas";
    canvas.width = 1;
    canvas.height = 1;
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `${row.displayLabel} · 当前结构区域局部放大`);
    const status = document.createElement("p");
    status.className = "dual-completion-crop-status";
    status.textContent = "展开后按需生成局部放大。";
    let requested = false;
    const markCropUnavailable = () => {
      requested = false;
      status.textContent = "局部放大不可用；请使用上方红框上下文或另开原始整页核对。";
    };
    const renderCrop = () => {
      if (drawCompletionCrop(image, canvas, row.normalizedBbox)) {
        status.textContent = "局部放大已由红框区域生成；请结合原始整页核对。";
      } else {
        markCropUnavailable();
      }
    };
    crop.addEventListener("toggle", () => {
      if (!crop.open || requested) return;
      requested = true;
      status.textContent = "正在生成局部放大…";
      if (image.complete) {
        if (image.naturalWidth) renderCrop();
        else markCropUnavailable();
      } else {
        image.addEventListener("load", renderCrop, {once: true});
        image.addEventListener("error", markCropUnavailable, {once: true});
      }
    });
    crop.append(summary, canvas, status);
    locator.append(preview, caption, crop);
    return locator;
  }

  function appendSmilesCandidateContext(document, fieldset, row) {
    if (row.field !== "resolved_smiles" || !row.smilesCandidates) return;
    const candidates = row.smilesCandidates;
    const context = document.createElement("section");
    context.className = "dual-smiles-candidate-context";
    appendText(document, context, "h6", "SMILES 候选上下文（不需双重补全）");
    const values = document.createElement("dl");
    [
      ["展开候选", candidates.expanded || "未提供"],
      ["未展开候选", candidates.unexpanded || "未提供"],
    ].forEach(([label, value]) => {
      const item = document.createElement("div");
      appendText(document, item, "dt", label);
      appendText(document, item, "dd", value);
      values.append(item);
    });
    context.append(values);
    appendText(
      document,
      context,
      "p",
      `${candidates.difference ? "候选存在差异" : "候选未标记差异"} · 候选来源：${candidates.selectedSource}`,
      "dual-smiles-candidate-note",
    );
    fieldset.append(context);
  }

  function appendResolutionSummary(document, fieldset, row) {
    if (row.resolvedSmilesStatus === "UNKNOWN") return;
    const summary = document.createElement("section");
    summary.className = `dual-resolution-summary ${row.resolvedSmilesStatus.toLowerCase()}`;
    appendText(document, summary, "strong", row.resolvedSmilesStatusLabel);
    if (row.resolvedSmilesStatus === "BLOCKED") {
      appendText(document, summary, "p", `gap reason：${row.gapReason}`);
      appendText(document, summary, "p", "value=null");
    } else {
      appendText(document, summary, "p", `value=${row.resolvedSmiles || "未提供"}`);
      if (row.resolvedSmilesStatus === "AI_PROVISIONAL") {
        appendText(
          document,
          summary,
          "p",
          `confidence ${row.confidence === null ? "未知" : row.confidence} · provenance ${row.provenanceSource}`,
        );
      }
    }
    if (row.actorProvenanceResidual) {
      appendText(document, summary, "p", `Actor provenance residual：${row.actorProvenanceResidual}`);
    }
    fieldset.append(summary);
  }

  function renderCompletion(document, parent, rows, handlers, model) {
    parent.replaceChildren();
    appendText(document, parent, "h4", "Chemical Completion Queue");
    const groups = groupCompletionRows(rows);
    groups.forEach((group, groupIndex) => {
      const form = document.createElement("form");
      form.className = "dual-completion-form";
      appendText(
        document,
        form,
        "p",
        "未填写条目保留待核对/未写入；仅提交本批次中完整填写的条目。",
        "dual-completion-partial-note",
      );
      appendText(document, form, "h5", `待补全批次 ${groupIndex + 1}`);
      const controls = [];
      group.rows.forEach((row, rowIndex) => {
        const fieldset = document.createElement("fieldset");
        const legend = document.createElement("legend");
        legend.textContent = `${row.displayLabel} · ${row.fieldLabel} · ${row.locatorLabel}`;
        fieldset.append(legend);
        appendResolutionSummary(document, fieldset, row);
        if (row.pdfPageUrl) {
          if (row.normalizedBbox) fieldset.append(completionLocator(document, row));
          const link = document.createElement("a");
          link.className = "dual-completion-source-link";
          link.href = row.pdfPageUrl;
          link.target = "_blank";
          link.rel = "noopener";
          link.setAttribute("aria-label", `${row.displayLabel} · 另开原始整页，不含红框`);
          link.textContent = "另开原始整页（不含红框） ↗";
          fieldset.append(link);
        }
        appendSmilesCandidateContext(document, fieldset, row);
        const value = document.createElement("input");
        value.className = "dual-completion-value";
        value.autocomplete = "off";
        const reason = document.createElement("textarea");
        reason.className = "dual-completion-reason";
        reason.rows = 2;
        const page = document.createElement("input");
        page.type = "number";
        page.min = "1";
        page.value = row.page ? String(row.page) : "";
        const figure = document.createElement("input");
        figure.autocomplete = "off";
        const control = {row, value, reason, page, figure, selectedCandidate: null, rowIndex};
        value.addEventListener("input", () => {
          control.selectedCandidate = null;
        });
        if (row.candidateSuggestions.length) {
          const suggestions = document.createElement("section");
          suggestions.className = "dual-completion-agent-candidates";
          appendText(document, suggestions, "strong", "Content Agent 候选（仅供研究者复核）");
          appendText(document, suggestions, "p", "候选不会自动成为权威决定；请核对原始 PDF 后再保存 AI_PROVISIONAL。", "dual-completion-agent-candidate-note");
          const list = document.createElement("ul");
          row.candidateSuggestions.forEach(candidate => {
            const item = document.createElement("li");
            appendText(document, item, "span", `${candidate.value} · confidence ${candidate.confidence === null ? "未知" : candidate.confidence} · ${candidate.provenanceLabel}`);
            appendText(document, item, "p", `${candidate.reason} · 第 ${candidate.page} 页${candidate.figureLabel ? ` · ${candidate.figureLabel}` : ""}`);
            const use = document.createElement("button");
            use.type = "button";
            use.className = "dual-parse-secondary";
            use.textContent = "采纳为 AI_PROVISIONAL（仍需核对）";
            use.addEventListener("click", () => {
              value.value = candidate.value;
              reason.value = candidate.reason;
              page.value = String(candidate.page);
              figure.value = candidate.figureLabel;
              control.selectedCandidate = candidate;
              value.focus();
            });
            item.append(use);
            list.append(item);
          });
          suggestions.append(list);
          fieldset.append(suggestions);
        }
        fieldset.append(
          labelledControl(document, `${row.fieldLabel} 补充值`, value),
          labelledControl(document, "PDF 核对理由", reason),
          labelledControl(document, "PDF 页码", page),
          labelledControl(document, "图、Scheme 或表号（可选）", figure),
        );
        form.append(fieldset);
        controls.push(control);
      });
      const actor = safeActor(handlers);
      const actorInput = document.createElement("input");
      actorInput.className = "dual-completion-actor";
      actorInput.autocomplete = "off";
      actorInput.value = actor.actorLabel;
      form.append(labelledControl(document, "本批次决定者", actorInput));
      const submit = document.createElement("button");
      submit.type = "submit";
      submit.className = "dual-parse-primary";
      submit.textContent = "保存本批次补全";
      form.append(submit);
      form.addEventListener("submit", event => {
        event.preventDefault();
        try {
          const completeControls = controls.filter(control => {
            const value = text(control.value.value, "");
            const reason = text(control.reason.value, "");
            const page = Number(control.page.value);
            return value && reason && Number.isInteger(page) && page >= 1;
          });
          if (!completeControls.length) {
            throw new Error("at least one complete correction required");
          }
          const corrections = completeControls.map(control => {
            const correction = {
              moleculeIndex: completionTargetByModel.get(control.row).moleculeIndex,
              field: control.row.field,
              value: control.value.value,
              reason: control.reason.value,
              pdfLocator: {page: Number(control.page.value), figureLabel: control.figure.value},
            };
            if (control.selectedCandidate) {
              correction.resolutionStatus = "AI_PROVISIONAL";
              correction.confidence = control.selectedCandidate.confidence;
              correction.provenance = {
                ...control.selectedCandidate.provenance,
              };
            }
            return correction;
          });
          handlers?.onCompletionSave?.(
            completionBatchRequest(
              group.target.studyId,
              group.target.versionToken,
              corrections,
              {...actor, actorLabel: actorInput.value},
            ),
            form,
          );
        } catch (error) {
          handlers?.onValidationError?.(
            error?.message === "at least one complete correction required"
              ? "请至少完整填写一项，并为该项填写补充值、PDF 页码与核对理由。"
              : "请为本批次填写决定者，并为已填写条目填写补充值、PDF 页码与核对理由。",
          );
        }
      });
      parent.append(form);
    });
    if (!groups.length) appendText(document, parent, "p", "当前没有缺失名称、局部标签或已解析 SMILES。", "dual-parse-empty");
  }

  function renderReconciliation(document, parent, items, handlers) {
    parent.replaceChildren();
    appendText(document, parent, "h4", "Reconciliation");
    items.forEach((item, index) => {
      const card = document.createElement("article");
      card.className = "dual-reconciliation-card";
      const heading = document.createElement("header");
      appendText(document, heading, "h5", `${item.kindLabel} ${index + 1}`);
      appendText(document, heading, "strong", item.statusLabel);
      card.append(heading);
      const candidates = document.createElement("div");
      candidates.className = "dual-candidate-grid";
      [["Generic candidate", item.genericCandidate], ["Chemical candidate", item.chemicalCandidate]].forEach(([label, value]) => {
        const pane = document.createElement("section");
        appendText(document, pane, "span", label);
        appendText(document, pane, "p", value);
        candidates.append(pane);
      });
      card.append(candidates);
      appendText(document, card, "p", item.locatorLabel, "dual-parse-freshness");
      if (item.pdfPageUrl) {
        const link = document.createElement("a");
        link.href = item.pdfPageUrl;
        link.target = "_blank";
        link.rel = "noopener";
        link.textContent = "打开原始 PDF 页仲裁 ↗";
        card.append(link);
      }
      const target = reconciliationTargetByModel.get(item);
      if (target && ["conflict", "needs_review", "single_lane_only", "stale"].includes(item.status)) {
        const form = document.createElement("form");
        form.className = "dual-reconciliation-form";
        const action = document.createElement("select");
        [["", "选择 PDF 仲裁动作"], ["pdf_resolved", "按 PDF 选择候选"], ["pdf_locator_only", "仅使用 PDF 定位"], ["reject_both", "拒绝两侧候选"]]
          .forEach(([value, label]) => {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = label;
            action.append(option);
          });
        const lane = document.createElement("select");
        [["", "不预选 lane"], ["generic", "Generic"], ["chemical", "Chemical"]].forEach(([value, label]) => {
          const option = document.createElement("option");
          option.value = value;
          option.textContent = label;
          lane.append(option);
        });
        lane.disabled = true;
        action.addEventListener("change", () => {
          lane.disabled = action.value !== "pdf_resolved";
          if (lane.disabled) lane.value = "";
        });
        const note = document.createElement("textarea");
        note.rows = 2;
        const page = document.createElement("input");
        page.type = "number";
        page.min = "1";
        page.value = item.page ? String(item.page) : "";
        const figure = document.createElement("input");
        figure.autocomplete = "off";
        const submit = document.createElement("button");
        submit.type = "submit";
        submit.textContent = "保存 PDF 仲裁";
        form.append(
          labelledControl(document, "仲裁动作", action),
          labelledControl(document, "采用的 lane（仅 PDF 选择候选时）", lane),
          labelledControl(document, "PDF 仲裁说明", note),
          labelledControl(document, "PDF 页码", page),
          labelledControl(document, "图、Scheme 或表号（可选）", figure),
          submit,
        );
        form.addEventListener("submit", event => {
          event.preventDefault();
          try {
            handlers?.onReconciliationSave?.(reconciliationRequest(
              target.studyId,
              target.objectId,
              target.registryDigest,
              {
                action: action.value,
                selectedLane: lane.value,
                note: note.value,
                pdfLocator: {page: Number(page.value), figureLabel: figure.value},
              },
              safeActor(handlers),
            ), form);
          } catch (_) {
            handlers?.onValidationError?.("请选择仲裁动作，并填写 PDF 页码与说明；选择候选时还需明确 lane。");
          }
        });
        card.append(form);
      }
      appendText(document, card, "p", `${item.actorLabel} · ${item.updatedLabel}`, "dual-parse-freshness");
      parent.append(card);
    });
    if (!items.length) appendText(document, parent, "p", "当前没有需要人工仲裁的双层差异。", "dual-parse-empty");
  }

  function render(document, mount, input, handlers) {
    if (!document || !mount) return;
    const model = input?.contractValid === true ? input : projectionModel(input);
    const honestRoot = mount.querySelector("#honest-progressive-summary");
    const studyRoot = mount.querySelector("#dual-study-status");
    const preflightRoot = mount.querySelector("#chemical-import-preflight");
    const completionRoot = mount.querySelector("#chemical-completion-queue");
    const reconciliationRoot = mount.querySelector("#reconciliation-list");
    if (!studyRoot || !preflightRoot || !completionRoot || !reconciliationRoot) return;
    const wiredHandlers = {
      ...(handlers || {}),
      onPreflightResult: payload => {
        renderPreflight(document, preflightRoot, importPreflightModel(payload), wiredHandlers);
      },
    };
    if (honestRoot) {
      renderHonestProgressive(document, honestRoot, model);
      renderInputCoverage(document, honestRoot, model.inputCoverage);
    }
    renderStatus(document, studyRoot, model, wiredHandlers);
    renderStudies(document, studyRoot, model, wiredHandlers);
    renderPreflight(document, preflightRoot, model.importPreflight, wiredHandlers);
    renderCompletion(document, completionRoot, model.completionQueue, wiredHandlers, model);
    renderReconciliation(document, reconciliationRoot, model.reconciliationItems, wiredHandlers);
  }

  async function load(projectId, request) {
    const requester = request || globalThis.fetch;
    if (typeof requester !== "function") throw new Error("request function required");
    const encoded = encodeURIComponent(required(projectId, "project id"));
    try {
      const response = await requester.call(globalThis, `/api/project/${encoded}/dual-parse`);
      if (!response.ok) {
        return projectionModel({
          schema_version: "dual-parse-projection.v2",
          status: "failed",
          failure_message: "双层解析状态读取失败；权威状态未更改。",
          retryable: true,
        });
      }
      return projectionModel(await response.json());
    } catch (_) {
      return projectionModel({
        schema_version: "dual-parse-projection.v2",
        status: "failed",
        failure_message: "网络不可用；双层解析状态未更改。",
        retryable: true,
      });
    }
  }

  return {
    availabilityModel,
    completionBatchRequest,
    importConfirmRequest,
    importPreflightRequest,
    load,
    projectionModel,
    reconciliationRequest,
    render,
  };
}));
