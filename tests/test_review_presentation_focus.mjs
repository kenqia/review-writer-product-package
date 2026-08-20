import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";

const sourcePath = new URL("../view/assets/dashboard/review-ui.js", import.meta.url);

test("Review presentation supports a manuscript focus within the existing review route", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(source, /hash === "#manuscript"/);
  assert.match(source, /rw-manuscript-focus/);
  assert.match(source, /reviewFocus = manuscriptFocused \? "manuscript"/);
  assert.match(source, /document\.getElementById\("manuscript-workspace"\)/);
});
