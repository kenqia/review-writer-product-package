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

test("Review keeps rejected edits in place and explains the discard protection", async () => {
  const source = await readFile(reviewPath, "utf8");

  assert.match(source, /ReviewManuscriptUI\.draftSaveErrorMessage\(outcome\)/);
  assert.match(source, /function markEditorDirty\(\)/);
  assert.match(source, /当前正文或修改理由尚未保存/);
  assert.match(source, /选择“取消”会留在本页继续编辑/);
});
