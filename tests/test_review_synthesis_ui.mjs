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

async function loadSynthesisWorkspace() {
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
  const figures = {route: "evidence-to-release.v1", source_figures: [], placeholders: [], locator_gaps: [], manuscript: {}};
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
