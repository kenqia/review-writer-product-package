(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ReviewManuscriptUI = api;
}(typeof window !== "undefined" ? window : null, function () {
  "use strict";

  const array = value => Array.isArray(value) ? value : [];
  const string = value => typeof value === "string" ? value : "";
  const url = value => string(value).startsWith("/") ? value : "";

  function decision(value) {
    if (!value || typeof value !== "object") return null;
    return {
      action: string(value.action),
      reason: string(value.reason),
      actor_label: string(value.actor_label),
    };
  }

  function binding(value) {
    const row = value && typeof value === "object" ? value : {};
    return {
      paper_evidence_ids: array(row.paper_evidence_ids).map(string).filter(Boolean),
      synthesis_ids: array(row.synthesis_ids).map(string).filter(Boolean),
    };
  }

  function section(value) {
    const row = value && typeof value === "object" ? value : {};
    return {
      section_id: string(row.section_id || row.id),
      heading: string(row.heading),
      body: string(row.body),
      status: string(row.status),
      reason: string(row.reason || row.reason_code),
      version_token: string(row.version_token),
      risk_classes: array(row.risk_classes || row.high_risk_reasons).map(string).filter(Boolean),
      claim_bindings: array(row.claim_bindings).map(binding),
      decision: decision(row.decision),
    };
  }

  function sectionId(value) {
    return section(value).section_id;
  }

  function evidence(value) {
    const row = value && typeof value === "object" ? value : {};
    const locator = row.locator && typeof row.locator === "object" ? row.locator : {};
    return {
      evidence_id: string(row.evidence_id),
      study_id: string(row.study_id),
      source_id: string(row.source_id),
      statement: string(row.statement),
      epistemic_type: string(row.epistemic_type),
      locator_label: string(row.locator_label || locator.section_or_item || locator.figure_or_table),
      locator_page: string(row.locator_page || locator.page),
      currentness: string(row.currentness || row.status),
      exact_quote: string(row.exact_quote || locator.exact_quote),
      pdf_page_url: url(row.pdf_page_url),
      parsed_text_url: url(row.parsed_text_url),
      risk_classes: array(row.risk_classes).map(string).filter(Boolean),
      status: string(row.status),
      reason: string(row.reason || row.reason_code),
      decision: decision(row.decision),
    };
  }

  function synthesis(value) {
    const row = value && typeof value === "object" ? value : {};
    return {
      synthesis_id: string(row.synthesis_id),
      proposition: string(row.proposition),
      comparison_axis: string(row.comparison_axis),
      applicability_boundary: string(row.applicability_boundary),
      uncertainty: string(row.uncertainty),
      risk_class: string(row.risk_class),
    };
  }

  function sourceFigure(value) {
    const row = value && typeof value === "object" ? value : {};
    return {
      figure_id: string(row.figure_id),
      study_id: string(row.study_id),
      title: string(row.title || row.figure_label),
      caption: string(row.caption),
      page_label: row.page ? `第 ${row.page} 页` : "",
      image_url: url(row.image_url),
      pdf_page_url: url(row.pdf_page_url),
      linked_evidence_ids: array(row.linked_evidence_ids || row.evidence_ids).map(string).filter(Boolean),
    };
  }

  function projectManuscript(payload) {
    const value = payload && typeof payload === "object" ? payload : {};
    return {
      route: string(value.route),
      status: string(value.status),
      reason: string(value.reason || value.reason_code),
      sections: array(value.sections).map(section),
      evidence: array(value.evidence || value.paper_evidence?.items).map(evidence),
      synthesis: array(value.synthesis || value.synthesis_claims?.items).map(synthesis),
      source_figures: array(value.source_figures || value.review_figures?.source_figures).map(sourceFigure),
    };
  }

  function buildEditRequest(currentSection, editedBody, reason) {
    const row = section(currentSection);
    return {
      section_id: row.section_id,
      edited_body: string(editedBody),
      reason: string(reason).trim(),
      version_token: row.version_token,
      actor_type: "simulated_researcher_agent",
      actor_label: "dashboard-playwright-researcher",
    };
  }

  async function saveEdit(options) {
    const response = await options.request.call(globalThis, options.url, {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(options.payload),
    });
    if (response.status === 409) {
      if (typeof options.onConflict === "function") options.onConflict();
      return {status: "conflict"};
    }
    if (!response.ok) return {status: "error", httpStatus: response.status};
    const payload = await response.json();
    if (typeof options.onSuccess === "function") options.onSuccess(payload);
    return {status: "saved", payload};
  }

  function contextForSection(manuscript, currentSection) {
    const safe = projectManuscript(manuscript);
    const row = section(currentSection);
    const evidenceIds = new Set(row.claim_bindings.flatMap(item => item.paper_evidence_ids));
    const synthesisIds = new Set(row.claim_bindings.flatMap(item => item.synthesis_ids));
    const visibleEvidence = safe.evidence.filter(item => evidenceIds.has(item.evidence_id));
    const visibleSynthesis = safe.synthesis.filter(item => synthesisIds.has(item.synthesis_id));
    const linkedEvidenceIds = new Set(visibleEvidence.map(item => item.evidence_id));
    const visibleFigures = safe.source_figures.filter(item => (
      !item.linked_evidence_ids.length
      || item.linked_evidence_ids.some(id => linkedEvidenceIds.has(id))
    ));
    return {evidence: visibleEvidence, synthesis: visibleSynthesis, source_figures: visibleFigures};
  }

  return {buildEditRequest, contextForSection, projectManuscript, saveEdit, sectionId};
}));
