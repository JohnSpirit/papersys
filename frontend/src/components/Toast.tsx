import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { AlertCircle, CheckCircle2, Info, X } from "lucide-react";
import { ToastContext, type ToastKind } from "@/components/toast-context";

interface Toast {
  id: number;
  kind: ToastKind;
  msg: string;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const toast = useCallback((msg: string, kind: ToastKind = "info") => {
    const id = Date.now() + Math.random();
    setToasts((current) => [...current, { id, kind, msg }]);
  }, []);
  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((item) => item.id !== id));
  }, []);
  const value = useMemo(() => ({ toast }), [toast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="fixed bottom-4 right-4 z-[100] flex w-[calc(100vw-2rem)] max-w-sm flex-col gap-2" aria-live="polite" aria-atomic="false">
        {toasts.map((item) => <ToastItem key={item.id} toast={item} onDismiss={() => dismiss(item.id)} />)}
      </div>
    </ToastContext.Provider>
  );
}

function ToastItem({ toast, onDismiss }: { toast: Toast; onDismiss: () => void }) {
  const [leaving, setLeaving] = useState(false);
  const [paused, setPaused] = useState(false);
  const dismissRef = useRef(onDismiss);
  useEffect(() => { dismissRef.current = onDismiss; }, [onDismiss]);

  useEffect(() => {
    if (paused) return;
    const duration = toast.kind === "error" ? 8000 : 4000;
    const leaveTimer = setTimeout(() => setLeaving(true), duration - 200);
    const dismissTimer = setTimeout(() => dismissRef.current(), duration);
    return () => {
      clearTimeout(leaveTimer);
      clearTimeout(dismissTimer);
    };
  }, [toast.id, toast.kind, paused]);

  const config = {
    success: { icon: <CheckCircle2 size={16} />, cls: "border-green-700 bg-green-900/95 text-green-100" },
    error: { icon: <AlertCircle size={16} />, cls: "border-red-700 bg-red-900/95 text-red-100" },
    info: { icon: <Info size={16} />, cls: "border-blue-700 bg-blue-900/95 text-blue-100" },
  }[toast.kind];

  return (
    <div
      className={`flex items-start gap-2 rounded border px-3 py-2 text-sm shadow-lg transition-opacity ${config.cls} ${leaving ? "opacity-0" : "opacity-100"}`}
      role={toast.kind === "error" ? "alert" : "status"}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
    >
      <span className="mt-0.5 shrink-0" aria-hidden="true">{config.icon}</span>
      <span className="min-w-0 flex-1 break-words">{toast.msg}</span>
      <button type="button" onClick={onDismiss} className="min-h-6 min-w-6 shrink-0 opacity-80 hover:opacity-100" aria-label="关闭通知">
        <X size={14} aria-hidden="true" />
      </button>
    </div>
  );
}
