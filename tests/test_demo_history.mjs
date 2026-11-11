import assert from "node:assert/strict";
import { parseHistory } from "../demo/dist/history.js";
import { samples } from "../demo/dist/data.js";
const sample = Object.values(samples)[0];
const record = {
  id: "test",
  savedAt: new Date().toISOString(),
  report: {
    ...sample,
    mode: "synthetic-example",
    comment: "合成复核",
    approved: false,
  },
};
assert.equal(parseHistory(null).length, 0);
assert.throws(() => parseHistory(""), /原记录已保留/);
assert.equal(
  parseHistory(JSON.stringify({ version: 1, items: [record] })).length,
  1,
);
assert.throws(() =>
  parseHistory(
    JSON.stringify({
      version: 1,
      items: [
        { ...record, report: { ...record.report, text: "真实学生内容" } },
      ],
    }),
  ),
);
assert.throws(() =>
  parseHistory(
    JSON.stringify({
      version: 1,
      items: [
        { ...record, report: { ...record.report, scores: [999, 0, 0, 0, 0] } },
      ],
    }),
  ),
);
assert.throws(() =>
  parseHistory(JSON.stringify({ version: 1, items: Array(11).fill(record) })),
);
console.log("PASS: synthetic-only history, score boundaries and capacity");
