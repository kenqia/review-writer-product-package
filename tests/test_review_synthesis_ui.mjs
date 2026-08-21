import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const sourcePath = new URL("../view/assets/dashboard/review-synthesis.js", import.meta.url);

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.listeners = new Map();
    this.className = "";
    this.textContent = "";
    this.value = "";
    this.name = "";
    this.type = "";
    this.required = false;
    this.disabled = false;
    this.role = "";
  }

  append(...nodes) {
    for (const node of nodes) {
      if (node instanceof FakeElement) {
        node.parentElement = this;
        this.children.push(node);
      }
    }
  }

  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  setAttribute(name, value) {
    this[name] = value;
  }

  click() {
    return this.listeners.get("click")?.({preventDefault() {}});
  }

  focus() {}
}

function descendants(node, predicate) {
  const result = [];
  for (const child of node.children) {
    if (predicate(child)) result.push(child);
    result.push(...descendants(child, predicate));
  }
  return result;
}

function one(root, predicate, description) {
  const matches = descendants(root, predicate);
  assert.equal(matches.length, 1, `expected one ${description}`);
  return matches[0];
}

function flush() {
  return new Promise(resolve => setImmediate(resolve));
}

async function loadSynthesisWorkspace({synthesisDecision = null, sourceFigures = [], placeholderRegistration = null} = {}) {
  const root = new FakeElement("div");
  const projectSelect = new FakeElement("select");
  projectSelect.value = "automated-qa-project";
  const elements = new Map([
    ["synthesis-workspace-root", root],
    ["project", projectSelect],
  ]);
  const requests = [];
  const protocol = {
    route: "evidence-to-release.v1",
    status: "needs_review",
    evidence_ready: true,
    protocol: {
      comparison_id: "comparison-1",
      comparison_objects: ["evidence-1"],
      axes: ["source-reported observation"],
      normalization_rules: ["Keep locators."],
      missing_value_policy: "Unknown stays unknown.",
      incomparability_rules: ["Single study."],
      counterevidence_rules: ["Record gaps."],
      claim_strength: "single-study case report",
      version_token: "protocol-token-1",
    },
  };
  const synthesis = {
    route: "evidence-to-release.v1",
    protocol_ready: true,
    items: [{
      synthesis_id: "synthesis-1",
      proposition: "A bounded source-reported case report.",
      comparison_axis: "source-reported observation",
      supporting_evidence_ids: ["evidence-1"],
      counter_evidence_ids: [],
      applicability_boundary: "single study only",
      uncertainty: "Chemical GAP",
      risk_class: "GAP",
      version_token: "synthesis-token-1",
      ...(synthesisDecision ? {decision: synthesisDecision} : {}),
    }],
    coverage: {status: "approved", axes: []},
  };
  const contracts = {
    route: "evidence-to-release.v1",
    synthesis_ready: true,
    items: [{
      section_id: "section-1",
      research_question: "What does this study report?",
      expected_synthesis: "Bounded case report.",
      figure_plan: [],
      version_token: "contract-token-1",
    }],
  };
  const figures = {route: "evidence-to-release.v1", source_figures: sourceFigures, placeholders: [], locator_gaps: [], manuscript: {}, placeholder_registration: placeholderRegistration};
  const window = {
    document: {
      readyState: "complete",
      createElement: tagName => new FakeElement(tagName),
      getElementById: id => elements.get(id) || null,
      addEventListener() {},
    },
    reviewProjectSelection: {
      getProjectId: value => value,
      getVisibleLabel: value => value,
    },
    ReviewAuditUI: {
      researcherLabel: (value, fallback) => value || fallback,
      humanStatus: value => value || "待核对",
      decisionActor: () => "automated QA",
    },
    reviewDecisionActor: () => ({
      actor_type: "simulated_researcher_agent",
      actor_label: "automated-qa",
    }),
  };
  window.window = window;
  window.ReviewSessionUI = {
    createProjectSurfaceCoordinator(options) {
      return {
        async refresh() {
          options.render(await options.load(options.getProjectId()));
        },
        async mutate(run, settings) {
          try {
            const result = await run(options.getProjectId());
            settings?.renderResult?.(result);
            return {status: "saved"};
          } catch (error) {
            settings?.onError?.(error);
            return {status: "error"};
          }
        },
        projectChanged() {},
      };
    },
  };
  const context = vm.createContext({
    window,
    document: window.document,
    fetch: async (url, options) => {
      if (options?.method === "PUT") {
        requests.push({url, body: JSON.parse(options.body)});
      }
      if (url.endsWith("comparison-protocol")) return {ok: true, json: async () => protocol};
      if (url.endsWith("/synthesis")) return {ok: true, json: async () => synthesis};
      if (url.endsWith("section-contracts")) return {ok: true, json: async () => contracts};
      return {ok: true, json: async () => figures};
    },
    console,
  });
  vm.runInContext(await readFile(sourcePath, "utf8"), context, {filename: sourcePath.pathname});
  await flush();
  return {root, requests};
}

test("Synthesis decisions use inline reasons and never require a browser prompt", async () => {
  const workspace = await loadSynthesisWorkspace();
  const reason = one(
    workspace.root,
    node => node.name === "comparison-protocol-decision-reason",
    "comparison protocol decision reason",
  );
  assert.equal(reason.required, true);
  const approve = one(
    workspace.root,
    node => node.name === "comparison-protocol-approve",
    "approve button",
  );
  approve.click();
  await flush();
  assert.equal(workspace.requests.length, 0);
  const status = one(
    workspace.root,
    node => node.role === "status" && node.parentElement?.children.includes(approve),
    "inline decision status",
  );
  assert.match(status.textContent, /理由/);

  reason.value = "Automated QA approves the bounded protocol.";
  approve.click();
  await flush();
  assert.deepEqual(workspace.requests, [{
    url: "/api/project/automated-qa-project/comparison-protocol",
    body: {
      action: "approve",
      reason: "Automated QA approves the bounded protocol.",
      version_token: "protocol-token-1",
      actor_type: "simulated_researcher_agent",
      actor_label: "automated-qa",
    },
  }]);

  const rejected = await loadSynthesisWorkspace();
  const rejectReason = one(
    rejected.root,
    node => node.name === "synthesis-decision-reason",
    "synthesis decision reason",
  );
  rejectReason.value = "Automated QA rejects the candidate.";
  const reject = one(
    rejected.root,
    node => node.name === "synthesis-reject",
    "reject button",
  );
  reject.click();
  await flush();
  assert.equal(rejected.requests.length, 1);
  assert.equal(rejected.requests[0].url, "/api/project/automated-qa-project/synthesis");
  assert.equal(rejected.requests[0].body.action, "reject");
});

test("Rejected synthesis claims remain reviewable with the current version token", async () => {
  const workspace = await loadSynthesisWorkspace({
    synthesisDecision: {
      action: "reject",
      reason: "Automated QA rejected the earlier review.",
      actor_type: "simulated_researcher_agent",
      actor_label: "automated-qa",
    },
  });
  const reason = one(
    workspace.root,
    node => node.name === "synthesis-decision-reason",
    "reopened synthesis decision reason",
  );
  assert.equal(reason.value, "Automated QA rejected the earlier review.");
  const approve = one(
    workspace.root,
    node => node.name === "synthesis-approve",
    "reopened synthesis approve button",
  );
  reason.value = "Automated QA approves the bounded, non-chemical case report.";
  approve.click();
  await flush();
  assert.deepEqual(workspace.requests, [{
    url: "/api/project/automated-qa-project/synthesis",
    body: {
      synthesis_id: "synthesis-1",
      action: "approve",
      reason: "Automated QA approves the bounded, non-chemical case report.",
      version_token: "synthesis-token-1",
      actor_type: "simulated_researcher_agent",
      actor_label: "automated-qa",
    },
  }]);
});

test("Candidate-only figure selection refuses unknown rights before sending an incomplete PUT", async () => {
  const workspace = await loadSynthesisWorkspace({
    sourceFigures: [{
      figure_id: "study-1:source-1:figure-1",
      candidate_only: true,
      study_id: "study-1",
      source_id: "source-1",
      page: 2,
      figure_label: "Figure 1",
      caption: "Reaction overview.",
      selection_status: "available",
      asset_sha256: "a".repeat(64),
      version_token: "candidate-figure-token",
      rights_context: {status: "unknown"},
      target_binding: {
        figure_id: "study-1:source-1:figure-1",
        asset_sha256: "a".repeat(64),
        manuscript_sha256: "b".repeat(64),
        section_id: "section-1",
        marker: "[source:source-1]",
        occurrence: 1,
      },
    }],
  });
  const select = one(
    workspace.root,
    node => node.name === "figure-rights-status",
    "candidate figure rights status",
  );
  assert.equal(select.value, "unknown");
  const choose = one(
    workspace.root,
    node => node.tagName === "BUTTON" && node.textContent === "选择原图",
    "candidate figure selection button",
  );
  choose.click();
  await flush();
  assert.equal(workspace.requests.length, 0);
  const status = one(
    workspace.root,
    node => node.role === "status" && /rights_status|cleared/.test(node.textContent),
    "candidate figure rights error",
  );
  assert.match(status.textContent, /cleared/);
});

test("Candidate-only figure selection sends cleared rights and preserves target binding", async () => {
  const workspace = await loadSynthesisWorkspace({
    sourceFigures: [{
      figure_id: "study-1:source-1:figure-1",
      candidate_only: true,
      study_id: "study-1",
      source_id: "source-1",
      page: 2,
      figure_label: "Figure 1",
      caption: "Reaction overview.",
      selection_status: "available",
      asset_sha256: "a".repeat(64),
      version_token: "candidate-figure-token",
      rights_context: {status: "unknown"},
      target_binding: {
        figure_id: "study-1:source-1:figure-1",
        asset_sha256: "a".repeat(64),
        manuscript_sha256: "b".repeat(64),
        section_id: "section-1",
        marker: "[source:source-1]",
        occurrence: 1,
      },
    }],
  });
  const select = one(
    workspace.root,
    node => node.name === "figure-rights-status",
    "candidate figure rights status",
  );
  select.value = "cleared";
  one(workspace.root, node => node.name === "figure-license-or-rights-basis", "rights basis").value = "CC BY 4.0";
  one(workspace.root, node => node.name === "figure-attribution", "figure attribution").value = "Source Figure Attribution: study-1:source-1:figure-1";
  one(workspace.root, node => node.name === "figure-rights-evidence-reference", "rights evidence reference").value = "rights-record-1";
  const choose = one(
    workspace.root,
    node => node.tagName === "BUTTON" && node.textContent === "选择原图",
    "candidate figure selection button",
  );
  choose.click();
  await flush();
  assert.deepEqual(workspace.requests, [{
    url: "/api/project/automated-qa-project/review-figures",
    body: {
      figure_id: "study-1:source-1:figure-1",
      selection_status: "selected",
      version_token: "candidate-figure-token",
      rights_status: "cleared",
      license_or_rights_basis: "CC BY 4.0",
      attribution: "Source Figure Attribution: study-1:source-1:figure-1",
      rights_evidence_reference: "rights-record-1",
      target_binding: {
        figure_id: "study-1:source-1:figure-1",
        asset_sha256: "a".repeat(64),
        manuscript_sha256: "b".repeat(64),
        section_id: "section-1",
        marker: "[source:source-1]",
        occurrence: 1,
      },
    },
  }]);
});

test("Candidate-only cleared rights require all rights fields before selection", async () => {
  const workspace = await loadSynthesisWorkspace({
    sourceFigures: [{
      figure_id: "study-1:source-1:figure-1",
      candidate_only: true,
      study_id: "study-1",
      source_id: "source-1",
      page: 2,
      figure_label: "Figure 1",
      caption: "Reaction overview.",
      selection_status: "available",
      asset_sha256: "a".repeat(64),
      version_token: "candidate-figure-token",
      rights_context: {status: "unknown"},
    }],
  });
  one(workspace.root, node => node.name === "figure-rights-status", "candidate figure rights status").value = "cleared";
  const choose = one(
    workspace.root,
    node => node.tagName === "BUTTON" && node.textContent === "选择原图",
    "candidate figure selection button",
  );
  choose.click();
  await flush();
  assert.equal(workspace.requests.length, 0);
  const status = one(
    workspace.root,
    node => node.role === "status" && /rights_status=cleared/.test(node.textContent),
    "missing cleared rights error",
  );
  assert.match(status.textContent, /license_or_rights_basis/);
});

test("Placeholder registration sends the complete human-bound schema envelope", async () => {
  const placeholder = {
    placeholder_id: "synthesis-figure-1",
    scientific_question: "How do the studies differ?",
    reader_takeaway: "Differences remain bounded.",
    panels: [{panel: "A", task: "Compare reported observations.", synthesis_claim_ids: ["synthesis-1"], source_figure_ids: []}],
    comparison_axis: "source-reported observation",
    required_labels_units: [],
    counter_evidence: [],
    forbidden_overclaims: ["Do not invent values."],
    unresolved_uncertainties: ["Rights remain unresolved."],
    caption_draft: "Synthesis figure placeholder.",
    target_size: "single-column",
    status: "awaiting_human_figure",
  };
  const workspace = await loadSynthesisWorkspace({
    placeholderRegistration: {
      placeholder,
      version_id: "v1",
      revision: 3,
      snapshot_digest: "snapshot-digest",
      version_token: "placeholder-registration-token",
      next_action: "HUMAN_ACTION_REQUIRED",
    },
  });
  const register = one(
    workspace.root,
    node => node.name === "placeholder-register",
    "placeholder registration button",
  );
  register.click();
  await flush();

  assert.deepEqual(workspace.requests, [{
    url: "/api/project/automated-qa-project/review-figures",
    body: {
      action: "register_placeholder",
      placeholder,
      version_token: "placeholder-registration-token",
      actor_type: "human_researcher",
      actor_label: "Dashboard researcher",
    },
  }]);
});
