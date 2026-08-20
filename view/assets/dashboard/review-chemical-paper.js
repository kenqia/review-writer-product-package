(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.ReviewChemicalPaperUI = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const fieldNames = new Set(["mol_idt", "resolved_smiles"]);
  const elementActions = new Set(["confirmed", "corrected", "not_applicable"]);
  const mutationTargetByMolecule = new WeakMap();

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

  function nonNegativeInteger(value) {
    return Number.isInteger(value) && value >= 0 ? value : null;
  }

  function positiveInteger(value) {
    return Number.isInteger(value) && value > 0 ? value : null;
  }

  function routes(projectId) {
    const encoded = encodeURIComponent(text(projectId, ""));
    const base = `/api/project/${encoded}/chemical-paper`;
    return {
      read: base,
      field: `${base}/field`,
      elements: `${base}/elements`,
    };
  }

  function reactionLabel(value) {
    return value === "unavailable_not_provided"
      ? "反应数据：导出包未提供"
      : "反应数据状态未提供";
  }

  function projectStatusLabel(value) {
    return ({
      missing: "尚无 Chemical Paper 导入",
      partial: "部分研究已导入",
      ready: "Chemical Paper 候选数据已就绪",
      needs_review: "Chemical Paper 候选数据需要核对",
    })[value] || "Chemical Paper 状态未提供";
  }

  function studyStatusLabel(value) {
    return ({
      missing: "尚未导入",
      ready: "候选数据已就绪",
      needs_review: "存在待核对字段",
    })[value] || "状态未提供";
  }

  function pdfBindingLabel(value) {
    return ({
      bound: "已绑定原始 PDF",
      missing: "尚未绑定原始 PDF",
      stale: "原始 PDF 绑定已失效",
    })[value] || "PDF 绑定状态未提供";
  }

  function elementReviewLabel(value) {
    return ({
      not_reviewed: "候选元素尚未审查",
      confirmed: "候选元素已确认",
      corrected: "候选元素已更正",
      not_applicable: "元素审查不适用",
    })[value] || "元素审查状态未提供";
  }

  function fileKindLabel(value) {
    return ({
      layout: "版面数据",
      markdown: "Markdown",
      molecule_info: "分子信息",
    })[value] || "未知文件种类";
  }

  function safePdfPageUrl(value) {
    const candidate = text(value, "");
    if (!candidate.startsWith("/api/project/") || candidate.startsWith("//")) return "";
    if (/(?:token|session|cookie)=/i.test(candidate)) return "";
    return candidate;
  }

  function fieldModel(row, missingFields, field) {
    const value = field === "resolved_smiles"
      ? safeSmilesText(row[field]) || ""
      : publicText(row[field], "");
    if (missingFields.has(field)) return {label: "待补充", editable: true};
    if (value) return {label: value, editable: false};
    return {label: "状态未提供", editable: false};
  }

  function candidateElements(value) {
    return array(value).flatMap(rowValue => {
      const row = object(rowValue);
      const symbol = text(row.symbol, "");
      const count = positiveInteger(row.count);
      if (!/^[A-Z][a-z]?$/.test(symbol) || count === null) return [];
      return [{symbol, count}];
    });
  }

  function fieldLabel(value) {
    return ({
      mol_idt: "mol_idt",
      resolved_smiles: "已解析 SMILES",
      smiles_expanded: "展开候选（历史）",
      smiles_unexpanded: "未展开候选（历史）",
    })[value] || "化学字段";
  }

  function hasPlausibleSmilesSyntax(value) {
    const withoutBrackets = value.replace(/\[[^\]\r\n]{1,200}\]/g, "");
    if (/[\[\]]/.test(withoutBrackets)) return false;
    if (!/(?:Cl|Br|[BCNOPSFIbcnops]|\[[^\]\r\n]{1,200}\])/.test(value)) return false;
    return /^(?:(?:Cl|Br)|[BCNOPSFIbcnops]|[0-9@+\-()=#$%.:\/\\*])+$/.test(withoutBrackets);
  }

  function safeSmilesText(value) {
    const candidate = text(value, "");
    if (!candidate || candidate.length > 1000) return null;
    if (/(?:^|\s)(?:\/(?:home|mnt|users|tmp|private)\/|[a-z]:\\)/i.test(candidate)) return null;
    if (!hasPlausibleSmilesSyntax(candidate)) return null;
    if (/\bV(?:2000|3000)\b|M\s+END/.test(candidate)) return null;
    if (/^\s*\{/.test(candidate)) return null;
    return candidate;
  }

  function smilesCandidatesModel(value) {
    const row = object(value);
    return {
      expanded: safeSmilesText(row.expanded),
      unexpanded: safeSmilesText(row.unexpanded),
      selectedSource: ({
        smiles_expanded: "展开候选",
        smiles_unexpanded: "未展开候选",
        researcher_correction: "研究者更正",
      })[row.selected_source] || "流程来源未选择",
      difference: row.candidate_difference === true,
    };
  }

  function historyModel(value) {
    const row = object(value);
    const isElements = row.kind === "element_review";
    const field = text(row.field, "");
    const historyValue = item => ["resolved_smiles", "smiles_expanded", "smiles_unexpanded"].includes(field)
      ? safeSmilesText(item) || "未提供"
      : publicText(item, "未提供");
    const prior = isElements
      ? elementReviewLabel(text(row.prior_value, text(row.prior_state, "未提供")))
      : historyValue(row.prior_value);
    const current = isElements
      ? elementReviewLabel(text(row.value, text(row.action, text(row.state, "未提供"))))
      : historyValue(row.value);
    return {
      label: isElements ? "元素审查" : fieldLabel(row.field),
      prior,
      current,
      actor: publicText(row.actor_label, "决定者未提供"),
      time: publicText(row.recorded_at, "决定时间未提供"),
      reason: publicText(row.reason, "理由未提供"),
      kind: isElements ? "elements" : "field",
    };
  }

  function moleculeModel(value, displayIndex, studyId) {
    const row = object(value);
    const page = positiveInteger(row.page);
    const bbox = array(row.bbox_normalized);
    const missingFields = new Set(array(row.missing_fields).filter(field => fieldNames.has(field)));
    const model = {
      displayLabel: `分子条目 ${displayIndex + 1}`,
      locatorLabel: page
        ? `第 ${page} 页 · ${bbox.length === 4 && bbox.every(Number.isFinite) ? "页面区域已定位" : "页面区域未提供"}`
        : "页码与页面区域未提供",
      pdfPageUrl: safePdfPageUrl(row.pdf_page_url),
      fields: {
        molIdt: fieldModel(row, missingFields, "mol_idt"),
        resolvedSmiles: fieldModel(row, missingFields, "resolved_smiles"),
      },
      smilesCandidates: smilesCandidatesModel(row.smiles_candidates),
      candidateElements: candidateElements(row.candidate_elements),
      elementReview: {
        state: text(row.element_review_state, "unknown"),
        label: elementReviewLabel(row.element_review_state),
      },
      history: array(row.history).map(historyModel),
    };
    const moleculeIndex = nonNegativeInteger(row.molecule_index);
    const versionToken = text(row.version_token, "");
    if (text(studyId, "") && moleculeIndex !== null && versionToken) {
      mutationTargetByMolecule.set(model, {
        studyId,
        moleculeIndex,
        versionToken,
      });
    }
    return model;
  }

  function missingFieldLabel(value) {
    const counts = Object.values(object(value));
    if (!counts.length || !counts.every(count => nonNegativeInteger(count) !== null)) {
      return "候选字段缺口数未提供";
    }
    return `${counts.reduce((total, count) => total + count, 0)} 个候选字段待核对`;
  }

  function studyModel(value, index) {
    const row = object(value);
    const backend = publicText(row.backend, "");
    const version = publicText(row.version, "");
    const status = text(row.status, text(row.chemical_import_status, "unknown"));
    const missingResolvedSmilesCount = nonNegativeInteger(row.missing_resolved_smiles_count);
    const aiAuthoredSmilesCount = nonNegativeInteger(row.ai_authored_smiles_count);
    const pageCount = positiveInteger(row.page_count);
    const moleculeCount = nonNegativeInteger(row.molecule_count);
    const studyId = text(row.study_id, "");
    return {
      displayLabel: `研究 ${index + 1}`,
      statusLabel: studyStatusLabel(status),
      pdfBindingLabel: pdfBindingLabel(text(row.pdf_binding_status, row.chemical_binding_status)),
      backendLabel: backend && version ? `${backend} · ${version}` : backend || version || "解析引擎与版本未提供",
      importedAtLabel: publicText(row.imported_at, "导入时间未提供"),
      fileKindsLabel: array(row.file_kinds).length
        ? array(row.file_kinds).map(fileKindLabel).join("、") : "文件种类未提供",
      pageCountLabel: pageCount === null ? "页数未提供" : `${pageCount} 页`,
      moleculeCountLabel: moleculeCount === null ? "分子条目数未提供" : `${moleculeCount} 个分子条目`,
      reactionLabel: reactionLabel(row.reaction_data_status),
      missingFieldLabel: missingFieldLabel(row.missing_field_counts),
      missingResolvedSmilesCount,
      missingResolvedSmilesLabel: missingResolvedSmilesCount === null
        ? "缺失已解析 SMILES 数未提供"
        : `缺失已解析 SMILES ${missingResolvedSmilesCount}`,
      aiAuthoredSmilesCount,
      aiAuthoredSmilesLabel: aiAuthoredSmilesCount === null
        ? "AI 生成 SMILES 数未提供"
        : `AI 生成 SMILES ${aiAuthoredSmilesCount}`,
      gaps: array(row.gaps).map(value => publicText(value, "")).filter(Boolean),
      molecules: array(row.molecules).map((molecule, moleculeIndex) => moleculeModel(molecule, moleculeIndex, studyId)),
    };
  }

  function buildChemicalPaperModel(input) {
    const value = object(input);
    const chemicalProjection = value.schema_version === "chemical-paper-projection.v2"
      && value.route === "chemical-paper-zip-only";
    const dualParseProjection = value.schema_version === "dual-parse-projection.v2"
      && (value.route === undefined || value.route === "dual-parse");
    if (!chemicalProjection && !dualParseProjection) {
      return {
        contractValid: false,
        route: "unknown",
        projectStatus: "unknown",
        projectStatusLabel: "Chemical Paper 安全投影合同未通过",
        summary: {
          studies: null,
          imported: null,
          molecules: null,
          unresolvedFields: null,
          missingResolvedSmilesCount: null,
          aiAuthoredSmilesCount: null,
          reactionLabel: "反应数据状态未提供",
        },
        studies: [],
      };
    }
    const summary = object(value.summary);
    return {
      contractValid: true,
      route: chemicalProjection ? text(value.route, "unknown") : "dual-parse",
      projectStatus: text(value.project_status, text(value.status, "unknown")),
      projectStatusLabel: projectStatusLabel(text(value.project_status, text(value.status, "unknown"))),
      summary: {
        studies: nonNegativeInteger(summary.studies),
        imported: nonNegativeInteger(summary.imported),
        molecules: nonNegativeInteger(summary.molecules),
        unresolvedFields: nonNegativeInteger(summary.unresolved_fields),
        missingResolvedSmilesCount: nonNegativeInteger(summary.missing_resolved_smiles_count),
        aiAuthoredSmilesCount: nonNegativeInteger(summary.ai_authored_smiles_count),
        reactionLabel: reactionLabel(summary.reaction_data_status),
      },
      studies: array(value.studies).map(studyModel),
    };
  }

  function required(value, label) {
    const normalized = text(value, "");
    if (!normalized) throw new Error(`${label} required`);
    return normalized;
  }

  function targetPayload(input) {
    const value = object(input);
    const moleculeIndex = nonNegativeInteger(value.moleculeIndex);
    if (moleculeIndex === null) throw new Error("molecule index required");
    return {
      study_id: required(value.studyId, "study id"),
      molecule_index: moleculeIndex,
    };
  }

  function decisionPayload(input) {
    const value = object(input);
    return {
      reason: required(value.reason, "reason"),
      actor_type: required(value.actorType, "actor type"),
      actor_label: required(value.actorLabel, "actor label"),
      version_token: required(value.versionToken, "version token"),
    };
  }

  function buildFieldMutation(input) {
    const value = object(input);
    const field = required(value.field, "field");
    if (!fieldNames.has(field)) throw new Error("unsupported field");
    return {
      ...targetPayload(value),
      field,
      value: required(value.value, "value"),
      ...decisionPayload(value),
    };
  }

  function parseCorrectedElements(value) {
    const rows = required(value, "corrected elements")
      .split(/[,\n]+/).map(item => item.trim()).filter(Boolean);
    const seen = new Set();
    return rows.map(item => {
      const match = /^([A-Z][a-z]?)\s*:\s*([1-9]\d*)$/.exec(item);
      if (!match || seen.has(match[1])) throw new Error("invalid corrected elements");
      seen.add(match[1]);
      return {symbol: match[1], count: Number(match[2])};
    });
  }

  function buildElementMutation(input) {
    const value = object(input);
    const action = required(value.action, "element action");
    if (!elementActions.has(action)) throw new Error("unsupported element action");
    const payload = {
      ...targetPayload(value),
      action,
      ...decisionPayload(value),
    };
    if (action === "corrected") payload.corrected_elements = parseCorrectedElements(value.correctedElements);
    return payload;
  }

  async function responseBody(response) {
    if (!response || typeof response.json !== "function") return {};
    try { return object(await response.json()); } catch (_) { return {}; }
  }

  async function saveMutation({request, url, payload, onConflict, onSuccess}) {
    const response = await request.call(globalThis, url, {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const body = await responseBody(response);
    if (response.status === 409 && body.error_code === "STALE_CHEMICAL_PAPER_STATE") {
      if (onConflict) onConflict(body);
      return {status: "conflict", code: body.error_code};
    }
    if (!response.ok) return {status: "failed", httpStatus: response.status};
    if (onSuccess) onSuccess(body);
    return {status: "saved"};
  }

  function appendText(document, parent, tag, value, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = value;
    parent.append(node);
    return node;
  }

  function appendFacts(document, parent, facts) {
    const list = document.createElement("dl");
    list.className = "chemical-paper-facts";
    facts.forEach(([label, value]) => {
      const item = document.createElement("div");
      appendText(document, item, "dt", label);
      appendText(document, item, "dd", value);
      list.append(item);
    });
    parent.append(list);
  }

  function labelledInput(document, labelText, name, value) {
    const label = document.createElement("label");
    label.append(document.createTextNode(labelText));
    const input = document.createElement("input");
    input.name = name;
    input.value = value || "";
    input.autocomplete = "off";
    label.append(input);
    return {label, input};
  }

  function appendHistory(document, parent, history) {
    const list = document.createElement("ul");
    list.className = "chemical-paper-history";
    if (!history.length) {
      appendText(document, list, "li", "尚无追加审查历史");
    } else {
      history.forEach(row => {
        appendText(
          document,
          list,
          "li",
          `${row.label} · ${row.prior} → ${row.current} · ${row.actor} · ${row.time} · ${row.reason}`,
        );
      });
    }
    parent.append(list);
  }

  function appendSmilesCandidates(document, parent, candidates) {
    const details = document.createElement("section");
    details.className = "chemical-paper-smiles-candidates";
    appendText(document, details, "strong", "SMILES 候选上下文（不需双重补全）");
    appendFacts(document, details, [
      ["展开候选", candidates.expanded || "未提供"],
      ["未展开候选", candidates.unexpanded || "未提供"],
      ["候选来源", candidates.selectedSource],
      ["候选差异", candidates.difference ? "存在差异" : "未标记差异"],
    ]);
    parent.append(details);
  }

  function appendCorrectionForm(document, parent, molecule, field, label, actor, handler, validationHandler) {
    const target = mutationTargetByMolecule.get(molecule);
    const form = document.createElement("form");
    form.className = "chemical-paper-correction-form";
    const value = labelledInput(document, `${label} 补充值`, "value", "");
    const actorInput = labelledInput(document, "决定者", "actor", actor.actor_label);
    const reason = labelledInput(document, "原 PDF 核对理由", "reason", "");
    const button = document.createElement("button");
    button.type = "submit";
    button.textContent = `保存 ${label}`;
    button.disabled = !target;
    if (!target) button.title = "等待安全版本保护信息";
    form.append(value.label, actorInput.label, reason.label, button);
    form.addEventListener("submit", event => {
      event.preventDefault();
      try {
        handler?.(buildFieldMutation({
          ...target,
          field,
          value: value.input.value,
          actorType: actor.actor_type,
          actorLabel: actorInput.input.value,
          reason: reason.input.value,
        }), form);
      } catch (_) {
        validationHandler?.("请填写补充值、决定者与原 PDF 核对理由。");
      }
    });
    parent.append(form);
  }

  function appendElementReview(document, parent, molecule, actor, handler, validationHandler) {
    const target = mutationTargetByMolecule.get(molecule);
    const details = document.createElement("details");
    details.className = "chemical-paper-element-review";
    appendText(document, details, "summary", "可选：审查结构候选元素");
    appendText(document, details, "p", "候选元素来自 Chemical Paper 导出；研究者决定前不代表科学确认，原始 PDF 始终优先。", "chemical-paper-caveat");
    appendText(document, details, "strong", molecule.elementReview.label);
    const elements = document.createElement("ul");
    elements.className = "chemical-paper-candidate-elements";
    const candidates = molecule.candidateElements.length
      ? molecule.candidateElements : [{symbol: "候选元素未提供", count: null}];
    candidates.forEach(row => appendText(document, elements, "li", row.count ? `${row.symbol} ${row.count}` : row.symbol));
    details.append(elements);
    const form = document.createElement("form");
    form.className = "chemical-paper-element-form";
    const stateLabel = document.createElement("label");
    stateLabel.append(document.createTextNode("审查决定"));
    const select = document.createElement("select");
    select.name = "action";
    [["confirmed", "确认候选元素"], ["corrected", "更正候选元素"], ["not_applicable", "不适用"]].forEach(([value, label]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.append(option);
    });
    stateLabel.append(select);
    const corrected = labelledInput(document, "更正元素（如 C: 2, H: 4）", "corrected_elements", "");
    const actorInput = labelledInput(document, "决定者", "actor", actor.actor_label);
    const reason = labelledInput(document, "核对理由", "reason", "");
    const button = document.createElement("button");
    button.type = "submit";
    button.textContent = "保存元素审查";
    button.disabled = !target;
    if (!target) button.title = "等待安全版本保护信息";
    form.append(stateLabel, corrected.label, actorInput.label, reason.label, button);
    form.addEventListener("submit", event => {
      event.preventDefault();
      try {
        handler?.(buildElementMutation({
          ...target,
          action: select.value,
          correctedElements: corrected.input.value,
          actorType: actor.actor_type,
          actorLabel: actorInput.input.value,
          reason: reason.input.value,
        }), form);
      } catch (_) {
        validationHandler?.("请填写决定者与核对理由；更正时还需填写元素和数量。");
      }
    });
    details.append(form);
    parent.append(details);
  }

  function appendMolecule(document, parent, molecule, actor, handlers) {
    const card = document.createElement("article");
    card.className = "chemical-paper-molecule";
    const header = document.createElement("header");
    header.className = "chemical-paper-molecule-header";
    appendText(document, header, "h6", molecule.displayLabel);
    appendText(document, header, "span", molecule.locatorLabel, "chemical-paper-status");
    card.append(header);
    if (molecule.pdfPageUrl) {
      const link = document.createElement("a");
      link.className = "chemical-paper-pdf-link";
      link.href = molecule.pdfPageUrl;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "打开原始 PDF 页核对 ↗";
      card.append(link);
    } else {
      appendText(document, card, "p", "原始 PDF 页入口未提供。", "chemical-paper-caveat");
    }
    appendFacts(document, card, [
      ["mol_idt", molecule.fields.molIdt.label],
      ["已解析 SMILES", molecule.fields.resolvedSmiles.label],
    ]);
    appendSmilesCandidates(document, card, molecule.smilesCandidates);
    if (molecule.fields.molIdt.editable) appendCorrectionForm(document, card, molecule, "mol_idt", "mol_idt", actor, handlers.onCorrectField, handlers.onValidationError);
    if (molecule.fields.resolvedSmiles.editable) appendCorrectionForm(document, card, molecule, "resolved_smiles", "已解析 SMILES", actor, handlers.onCorrectField, handlers.onValidationError);
    appendElementReview(document, card, molecule, actor, handlers.onReviewElements, handlers.onValidationError);
    appendHistory(document, card, molecule.history);
    parent.append(card);
  }

  function appendStudy(document, parent, study, actor, handlers) {
    const card = document.createElement("article");
    card.className = "chemical-paper-study";
    const header = document.createElement("header");
    header.className = "chemical-paper-study-header";
    appendText(document, header, "h5", study.displayLabel);
    appendText(document, header, "span", study.statusLabel, "chemical-paper-status");
    card.append(header);
    appendFacts(document, card, [
      ["PDF 绑定", study.pdfBindingLabel],
      ["解析引擎 / 版本", study.backendLabel],
      ["导入时间", study.importedAtLabel],
      ["文件种类", study.fileKindsLabel],
      ["页数", study.pageCountLabel],
      ["分子条目", study.moleculeCountLabel],
      ["反应数据", study.reactionLabel],
      ["字段缺口", study.missingFieldLabel],
      ["缺失已解析 SMILES", study.missingResolvedSmilesLabel],
      ["AI 生成 SMILES", study.aiAuthoredSmilesLabel],
    ]);
    const gaps = document.createElement("ul");
    gaps.className = "chemical-paper-gaps";
    (study.gaps.length ? study.gaps : ["当前安全投影未提供额外缺口。"])
      .forEach(value => appendText(document, gaps, "li", value));
    card.append(gaps);
    const details = document.createElement("details");
    details.className = "chemical-paper-molecules";
    appendText(document, details, "summary", `查看分子字段 · ${study.moleculeCountLabel}`);
    const molecules = document.createElement("div");
    molecules.className = "chemical-paper-molecule-list";
    study.molecules.forEach(molecule => appendMolecule(document, molecules, molecule, actor, handlers));
    if (!study.molecules.length) appendText(document, molecules, "p", "当前未提供分子条目安全投影。", "chemical-paper-empty");
    details.append(molecules);
    card.append(details);
    parent.append(card);
  }

  function renderChemicalPaper(rootNode, input, handlers, actor) {
    if (!rootNode || typeof document === "undefined") return;
    const model = buildChemicalPaperModel(input);
    rootNode.replaceChildren();
    const summary = document.createElement("section");
    summary.className = "chemical-paper-summary";
    appendText(document, summary, "strong", model.projectStatusLabel);
    appendText(
      document,
      summary,
      "span",
      `${model.summary.imported ?? "—"} / ${model.summary.studies ?? "—"} 篇已导入 · ${model.summary.molecules ?? "—"} 个分子条目 · ${model.summary.unresolvedFields ?? "—"} 个候选字段待核对`,
    );
    appendText(document, summary, "span", model.summary.reactionLabel);
    rootNode.append(summary);
    const list = document.createElement("div");
    list.className = "chemical-paper-study-list";
    const safeActor = {
      actor_type: text(actor?.actor_type, "human_researcher"),
      actor_label: text(actor?.actor_label, "研究者"),
    };
    model.studies.forEach(study => appendStudy(document, list, study, safeActor, handlers || {}));
    if (!model.studies.length) appendText(document, list, "p", "Chemical Paper 安全投影尚未提供；不会从旧解析结果推断状态。", "chemical-paper-empty");
    rootNode.append(list);
  }

  return {
    buildChemicalPaperModel,
    buildElementMutation,
    buildFieldMutation,
    renderChemicalPaper,
    routes,
    saveMutation,
  };
}));
