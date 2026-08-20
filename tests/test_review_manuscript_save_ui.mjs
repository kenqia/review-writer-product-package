import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const sourcePath = new URL("../view/assets/dashboard/review-manuscript.js", import.meta.url);
const reviewPath = new URL("../view/assets/dashboard/review.html", import.meta.url);

async function loadManuscriptUI() {
  const context = vm.createContext({
    module: {exports: {}},
    exports: {},
  });
  vm.runInContext(await readFile(sourcePath, "utf8"), context, {filename: sourcePath.pathname});
  return context.module.exports;
}

test("Draft save preserves the high-risk rejection code and gives a next step", async () => {
  const ui = await loadManuscriptUI();
  const outcome = await ui.saveEdit({
    request: async () => ({
      status: 400,
      ok: false,
      text: async () => "<p>Message: invalid draft payload: HIGH_RISK_EDIT_PENDING.</p>",
    }),
    url: "/api/project/automated-qa/draft",
    payload: {section_id: "section-1"},
  });

  assert.equal(outcome.status, "error");
  assert.equal(outcome.httpStatus, 400);
  assert.equal(outcome.errorCode, "HIGH_RISK_EDIT_PENDING");
  assert.match(
    ui.draftSaveErrorMessage(outcome),
    /直接修改章节正文.*当前输入仍保留/,
  );
});

test("Draft save exposes a clear fallback for other rejected payloads", async () => {
  const ui = await loadManuscriptUI();
  const outcome = await ui.saveEdit({
    request: async () => ({
      status: 400,
      ok: false,
      text: async () => "<p>Message: invalid draft payload: APPROVAL_REASON_REQUIRED.</p>",
    }),
    url: "/api/project/automated-qa/draft",
    payload: {section_id: "section-1"},
  });

  assert.equal(outcome.errorCode, "APPROVAL_REASON_REQUIRED");
  assert.match(ui.draftSaveErrorMessage(outcome), /填写修改理由.*当前输入仍保留/);
});

test("Draft approval records the researcher by default and reserves simulation for explicit QA", async () => {
  const ui = await loadManuscriptUI();
  const currentSection = {section_id: "section-1", version_token: "token-1"};

  assert.deepEqual(
    JSON.parse(JSON.stringify(ui.buildEditRequest(currentSection, "Edited body.", "Evidence checked."))),
    {
      section_id: "section-1",
      edited_body: "Edited body.",
      reason: "Evidence checked.",
      version_token: "token-1",
      actor_type: "human_researcher",
      actor_label: "研究者",
    },
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(ui.buildEditRequest(
      currentSection,
      "Edited body.",
      "Automated QA.",
      {actor_type: "simulated_researcher_agent", actor_label: "dashboard-playwright-reviewer"},
    ))),
    {
      section_id: "section-1",
      edited_body: "Edited body.",
      reason: "Automated QA.",
      version_token: "token-1",
      actor_type: "simulated_researcher_agent",
      actor_label: "dashboard-playwright-reviewer",
    },
  );
});

test("Legacy simulated approval exposes an explicit human re-confirm payload only", async () => {
  const ui = await loadManuscriptUI();
  const currentSection = {
    section_id: "section-1",
    version_token: "token-1",
    legacy_simulated_reconfirm_required: true,
  };

  assert.equal(
    ui.projectManuscript({route: "evidence-to-release.v1", sections: [currentSection]}).sections[0]
      .legacy_simulated_reconfirm_required,
    true,
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(ui.buildEditRequest(
      currentSection,
      "Unchanged, source-bound body.",
      "I independently re-checked the source-bound text.",
      undefined,
      {reconfirmSimulatedApproval: true},
    ))),
    {
      section_id: "section-1",
      edited_body: "Unchanged, source-bound body.",
      reason: "I independently re-checked the source-bound text.",
      version_token: "token-1",
      actor_type: "human_researcher",
      actor_label: "研究者",
      reconfirm_simulated_approval: true,
    },
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(ui.buildEditRequest(
      currentSection,
      "Unchanged, source-bound body.",
      "I independently re-checked the source-bound text.",
    ))),
    {
      section_id: "section-1",
      edited_body: "Unchanged, source-bound body.",
      reason: "I independently re-checked the source-bound text.",
      version_token: "token-1",
      actor_type: "human_researcher",
      actor_label: "研究者",
    },
  );
});

test("Review keeps rejected edits in place and explains the discard protection", async () => {
  const source = await readFile(reviewPath, "utf8");

  assert.match(source, /ReviewManuscriptUI\.draftSaveErrorMessage\(outcome\)/);
  assert.match(source, /function markEditorDirty\(\)/);
  assert.match(source, /当前正文或修改理由尚未保存/);
  assert.match(source, /选择“取消”会留在本页继续编辑/);
  assert.match(
    source,
    /buildEditRequest\(\s*activeSection,\s*submittedBody,\s*reason,\s*chemicalPaperActor\(\),\s*\{reconfirmSimulatedApproval: legacyReconfirm\},/,
  );
  assert.match(source, /以研究者身份重新确认本节/);
  assert.match(source, /legacy_simulated_reconfirm_required/);
  assert.match(source, /chemicalPaperActor\(\)\.actor_type === 'human_researcher'/);
});
