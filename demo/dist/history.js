import { samples } from "./data.js";
export const HISTORY_KEY = "inkwise.demo.history.v1";
export function parseHistory(raw) {
  if (raw === null) return [];
  if (!raw) throw Error("演示历史格式不正确，原记录已保留。");
  if (raw.length > 400000) throw Error("演示历史超过容量限制，原记录已保留。");
  const value = JSON.parse(raw);
  if (
    value.version !== 1 ||
    !Array.isArray(value.items) ||
    value.items.length > 10
  )
    throw Error("演示历史格式不正确，原记录已保留。");
  for (const item of value.items) {
    const r = item?.report,
      s = Object.values(samples).find(
        (s) =>
          s.text === r?.text && s.title === r?.title && s.policy === r?.policy,
      );
    if (
      !s ||
      r.mode !== "synthetic-example" ||
      typeof r.approved !== "boolean" ||
      typeof r.comment !== "string" ||
      r.comment.length > 2000 ||
      JSON.stringify(r.weights) !== JSON.stringify(s.weights) ||
      JSON.stringify(r.feedback) !== JSON.stringify(s.feedback) ||
      r.rubric !== s.rubric ||
      typeof item.id !== "string" ||
      item.id.length > 64 ||
      !Number.isFinite(Date.parse(item.savedAt)) ||
      (r.scores !== null &&
        (!Array.isArray(r.scores) ||
          r.scores.length !== 5 ||
          r.scores.some(
            (v, i) => !Number.isInteger(v) || v < 0 || v > s.weights[i],
          )))
    )
      throw Error("历史记录内容校验失败，未载入原文。");
  }
  return value.items;
}
