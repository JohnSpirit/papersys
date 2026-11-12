import { createContext, useContext } from "react";

export type ToastKind = "success" | "error" | "info";

export interface ToastApi {
  toast: (msg: string, kind?: ToastKind) => void;
}

export const ToastContext = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const c = useContext(ToastContext);
  if (!c) throw new Error("useToast must be used within ToastProvider");
  return c;
}
