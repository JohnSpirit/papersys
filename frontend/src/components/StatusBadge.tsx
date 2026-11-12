import type { PaperStatus } from "@/lib/api";

const LABEL: Record<PaperStatus, string> = {
  missing: "未执行",
  processing: "处理中",
  ok: "完成",
  partial: "部分完成",
  error: "失败",
  manual_hold: "待人工处理",
  skipped: "已跳过",
};

const CLASS: Record<PaperStatus, string> = {
  missing: "badge-gray",
  processing: "badge-blue",
  ok: "badge-green",
  partial: "badge-amber",
  error: "badge-red",
  manual_hold: "badge-amber",
  skipped: "badge-gray",
};

export function StatusBadge({ status }: { status: PaperStatus }) {
  return <span className={CLASS[status]}>{LABEL[status]}</span>;
}

const STEP_LABELS = {
  status_pdf: "入库",
  status_parsed: "解析",
  status_enrich: "富化",
  status_translated: "翻译",
  status_summary: "总结",
  status_embedded: "向量",
} as const;

type StatusPaper = Record<keyof typeof STEP_LABELS, PaperStatus>;

export function PaperStatusRow({ p }: { p: StatusPaper }) {
  const steps = Object.entries(STEP_LABELS).map(([field, label]) => ({
    label,
    status: p[field as keyof typeof STEP_LABELS],
  }));
  const details = steps.map((step) => `${step.label}：${LABEL[step.status]}`).join("；");
  const done = steps.filter((step) => step.status === "ok" || step.status === "skipped").length;
  const processing = steps.some((step) => step.status === "processing");
  const failed = steps.some((step) => step.status === "error");
  const held = steps.some((step) => step.status === "manual_hold");
  const partial = steps.some((step) => step.status === "partial");
  const label = processing ? "处理中" : failed ? "有失败" : held ? "待人工" : partial ? "部分完成" : `${done}/6 完成`;
  const cls = processing ? "badge-blue" : failed ? "badge-red" : held || partial ? "badge-amber" : done === 6 ? "badge-green" : "badge-gray";
  return <span className={cls} title={details} aria-label={details}>{label}</span>;
}
