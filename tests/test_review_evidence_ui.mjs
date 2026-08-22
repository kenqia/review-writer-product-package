import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const sourcePath = new URL("../view/assets/dashboard/review-evidence.js", import.meta.url);

test("Evidence page exposes the read-only Decision Bundle caller", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(source, /decision-bundle/);
  assert.match(source, /decision-bundle-panel/);
  assert.match(source, /expected_write_set/);
  assert.match(source, /HUMAN_ACTION_REQUIRED/);
});

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.listeners = new Map();
    this.className = "";
    this.textContent = "";
    this.value = "";
    this.hidden = false;
    this.disabled = false;
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

function flush() {
  return new Promise(resolve => setImmediate(resolve));
}

async function loadEvidenceWorkspace() {
  const root = new FakeElement("div");
  const shell = new FakeElement("section");
  const status = new FakeElement("p");
  const message = new FakeElement("p");
  const decisionBundlePanel = new FakeElement("section");
  const decisionBundleRoot = new FakeElement("div");
  const decisionBundleStatus = new FakeElement("span");
  const decisionBundleMessage = new FakeElement("p");
  const projectSelect = new FakeElement("select");
  projectSelect.value = "automated-qa-project";
  const elements = new Map([
    ["evidence-workspace-root", root],
    ["evidence-synthesis-workspace", shell],
    ["evidence-workspace-status", status],
    ["evidence-workspace-message", message],
    ["decision-bundle-panel", decisionBundlePanel],
    ["decision-bundle-root", decisionBundleRoot],
    ["decision-bundle-status", decisionBundleStatus],
    ["decision-bundle-message", decisionBundleMessage],
    ["project", projectSelect],
  ]);
  const requests = [];
  const calls = [];
  const payload = {
    route: "evidence-to-release.v1",
    status: "needs_review",
    source_pdf_descriptors: {status: "current"},
    items: [{
      evidence_id: "evidence-1",
      statement: "A source-bound evidence statement.",
      status: "needs_review",
      study_label: "Automated QA study",
      source_id: "source-1",
      epistemic_type: "experimental_observation",
      locator: {page: 1, section_or_item: "Results", exact_quote: "Quoted source text."},
      risk_classes: [],
      version_token: "version-token-1",
    }],
  };
  const decisionBundle = {
    schema_version: "decision-bundle.v1",
    status: "HUMAN_ACTION_REQUIRED",
    reason_code: "SOURCE_ROLE_HUMAN_ACTION_REQUIRED",
    current: {version_id: "v1", revision: 7},
    revision: 7,
    write_mode: "NONE",
    current_unchanged: true,
    decision_options: [{decision_id: "SOURCE_IDENTITY", label: "确认 MAIN/SI 来源身份", requires_human: true}],
    expected_write_set: ["01_evidence/source_truth/*/parse_quality.json"],
    conflicts: [{component: "source", code: "SOURCE_ROLE_HUMAN_ACTION_REQUIRED"}],
  };
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
      calls.push(url);
      if (url.endsWith("/decision-bundle")) return {ok: true, json: async () => decisionBundle};
      if (options?.method === "PUT") {
        requests.push({url, body: JSON.parse(options.body)});
      }
      return {ok: true, json: async () => payload};
    },
    console,
  });
  vm.runInContext(await readFile(sourcePath, "utf8"), context, {filename: sourcePath.pathname});
  await flush();
  return {root, requests, calls, decisionBundleRoot, decisionBundleStatus};
}

function byText(root, text) {
  const result = descendants(root, node => node.tagName === "BUTTON" && node.textContent === text);
  assert.equal(result.length, 1, `expected one ${text} button`);
  return result[0];
}

function control(root, name) {
  const result = descendants(root, node => node.name === name);
  assert.equal(result.length, 1, `expected one ${name} control`);
  return result[0];
}

function status(root) {
  const result = descendants(root, node => node.role === "status");
  assert.equal(result.length, 1, "expected one decision status region");
  return result[0];
}

test("Evidence decisions use card controls and send only contract-valid payloads", async () => {
  const approve = await loadEvidenceWorkspace();
  const approveReason = control(approve.root, "evidence-decision-reason");
  assert.equal(approveReason.required, true);
  byText(approve.root, "批准").click();
  await flush();
  assert.equal(approve.requests.length, 0);
  assert.match(status(approve.root).textContent, /核对理由/);
  approveReason.value = "Automated QA confirms this source-bound evidence.";
  byText(approve.root, "批准").click();
  await flush();
  assert.equal(approve.requests.length, 1);
  assert.deepEqual(approve.requests[0].body, {
    evidence_id: "evidence-1",
    action: "approve",
    reason: "Automated QA confirms this source-bound evidence.",
    version_token: "version-token-1",
    actor_type: "simulated_researcher_agent",
    actor_label: "automated-qa",
  });

  const reject = await loadEvidenceWorkspace();
  control(reject.root, "evidence-decision-reason").value = "Automated QA rejects this evidence.";
  byText(reject.root, "拒绝").click();
  await flush();
  assert.equal(reject.requests.length, 1);
  assert.equal("replacement_statement" in reject.requests[0].body, false);

  const revise = await loadEvidenceWorkspace();
  byText(revise.root, "修改后批准").click();
  await flush();
  const replacement = control(revise.root, "evidence-replacement-statement");
  assert.equal(replacement.hidden, false);
  assert.equal(replacement.required, true);
  assert.equal(revise.requests.length, 0);
  control(revise.root, "evidence-decision-reason").value = "Automated QA corrects the wording.";
  byText(revise.root, "修改后批准").click();
  await flush();
  assert.equal(revise.requests.length, 0);
  assert.match(status(revise.root).textContent, /修改后的证据表述/);
  replacement.value = "A corrected, source-bound evidence statement.";
  byText(revise.root, "修改后批准").click();
  await flush();
  assert.equal(revise.requests.length, 1);
  assert.deepEqual(revise.requests[0].body, {
    evidence_id: "evidence-1",
    action: "revise_and_approve",
    reason: "Automated QA corrects the wording.",
    replacement_statement: "A corrected, source-bound evidence statement.",
    version_token: "version-token-1",
    actor_type: "simulated_researcher_agent",
    actor_label: "automated-qa",
  });
});

test("Decision Bundle is consumed through the public route and stays read-only", async () => {
  const loaded = await loadEvidenceWorkspace();
  assert.equal(loaded.calls.filter(url => url.endsWith("/decision-bundle")).length, 1);
  assert.equal(loaded.decisionBundleStatus.textContent, "等待研究者决策");
  const rendered = descendants(loaded.decisionBundleRoot, node => node.textContent)
    .map(node => node.textContent)
    .join("\n");
  assert.match(rendered, /revision：7/);
  assert.match(rendered, /确认 MAIN\/SI 来源身份/);
  assert.match(rendered, /parse_quality/);
  assert.equal(descendants(loaded.decisionBundleRoot, node => node.tagName === "BUTTON").length, 0);
});
