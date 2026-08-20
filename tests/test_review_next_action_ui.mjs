import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";

const reviewPath = new URL("../view/assets/dashboard/review.html", import.meta.url);

test("Overview next action gives the drafting stage a plain-language jump to the manuscript workspace", async () => {
  const source = await readFile(reviewPath, "utf8");

  assert.match(source, /id="overview-next-action-guide"/);
  assert.match(source, /id="overview-next-action-go"/);
  assert.match(source, /function overviewNextActionGuide\(\)/);
  assert.match(source, /activeStage === 'drafting'/);
  assert.match(source, /打开正文工作室/);
  assert.match(source, /填写修改原因/);
  assert.match(source, /workspace: draftPayload\.available \? 'manuscript' : ''/);
  assert.match(source, /setWorkspace\(guide\.workspace\)/);
  assert.match(source, /window\.location\.hash = 'manuscript'/);
  assert.match(source, /nextActionButton\.disabled = busy/);
});
