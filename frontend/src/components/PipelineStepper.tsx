import type { Paper, PaperStatus } from "@/lib/api";
import { Check, Loader2, X, CircleDashed, PauseCircle } from "lucide-react";

/**
 * 步骤式流水线状态面板（参考 MinerU gradio 的 stepper 设计）：
 * done / active / pending / error / hold / partial / skip 七态，skip 表示该论文禁用步骤。
 */

type StepState = "done" | "active" | "pending" | "error" | "hold" | "partial" | "skip";

interface StepDef {
  key: string;
  label: string;
  field?: keyof Paper;
}

const ORDER: StepDef[] = [
  { key: "pdf",       label: "入库", field: "status_pdf" },
  { key: "parsed",    label: "解析", field: "status_parsed" },
  { key: "enrich",    label: "富化", field: "status_enrich" },
  { key: "embed",     label: "向量", field: "status_embedded" },
  { key: "translate", label: "翻译", field: "status_translated" },
  { key: "summary",   label: "总结", field: "status_summary" },
];

function normalize(status: PaperStatus | undefined): StepState {
  switch (status) {
    case "ok": return "done";
    case "partial": return "partial";
    case "processing": return "active";
    case "error": return "error";
    case "manual_hold": return "hold";
    case "skipped": return "skip";
    default: return "pending";
  }
}

export function PipelineStepper({ p }: { p: Paper }) {
  const steps: { key: string; label: string; state: StepState }[] = ORDER.map((o) => {
    const status = o.field ? (p[o.field] as PaperStatus | undefined) : undefined;
    return { key: o.key, label: o.label, state: normalize(status) };
  });

  const latestActive = steps.some((s) => s.state === "active");

  return (
    <div className="card">
      <div className="flex items-center gap-1 flex-wrap">
        {steps.map((s, i) => (
          <div key={s.key} className="flex items-center gap-1">
            {i > 0 && <div className="w-4 h-px bg-side-border" />}
            <StepChip label={s.label} state={s.state} />
          </div>
        ))}
        {latestActive && <span className="text-xs text-blue-300 ml-auto animate-pulse">处理中…</span>}
      </div>
    </div>
  );
}

function StepChip({ label, state }: { label: string; state: StepState }) {
  const base = "inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs border transition-colors";
  switch (state) {
    case "done":
      return (
        <span className={`${base} bg-green-800/30 text-green-200 border-green-700/60`} title="已完成">
          <Check size={11} /> {label}
        </span>
      );
    case "active":
      return (
        <span className={`${base} bg-blue-800/40 text-blue-200 border-blue-600/60 animate-pulse`} title="处理中">
          <Loader2 size={11} className="animate-spin" /> {label}
        </span>
      );
    case "error":
      return (
        <span className={`${base} bg-red-800/40 text-red-200 border-red-700/60`} title="失败">
          <X size={11} /> {label}
        </span>
      );
    case "partial":
      return (
        <span className={`${base} bg-amber-800/30 text-amber-200 border-amber-600/60`} title="部分成功">
          <Check size={11} /> {label}*
        </span>
      );
    case "hold":
      return (
        <span className={`${base} bg-purple-800/30 text-purple-200 border-purple-600/60`} title="等待人工介入">
          <PauseCircle size={11} /> {label}
        </span>
      );
    case "skip":
      return (
        <span className={`${base} bg-gray-800/40 text-gray-300 border-gray-700/40`} title="此论文禁用该步骤">
          <CircleDashed size={11} /> {label}
        </span>
      );
    default:
      return (
        <span className={`${base} bg-gray-800/40 text-gray-400 border-gray-700/40`} title="等待中">
          <CircleDashed size={11} /> {label}
        </span>
      );
  }
}
