import { AlertCircle, RefreshCw } from "lucide-react";

export function LoadingState({ label = "加载中…" }: { label?: string }) {
  return (
    <div className="card flex items-center justify-center gap-2 py-10 text-sm text-gray-400" role="status">
      <RefreshCw size={16} className="animate-spin" aria-hidden="true" />
      {label}
    </div>
  );
}

export function EmptyState({ title, detail }: { title: string; detail?: string }) {
  return (
    <div className="card py-10 text-center" role="status">
      <p className="text-sm text-gray-300">{title}</p>
      {detail && <p className="mt-1 text-xs text-gray-400">{detail}</p>}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="card flex flex-wrap items-center gap-3 border-red-800 text-sm text-red-200" role="alert">
      <AlertCircle size={16} className="shrink-0" aria-hidden="true" />
      <span className="min-w-0 flex-1 break-words">{message}</span>
      {onRetry && (
        <button type="button" className="btn text-xs" onClick={onRetry}>
          <RefreshCw size={13} aria-hidden="true" /> 重试
        </button>
      )}
    </div>
  );
}
