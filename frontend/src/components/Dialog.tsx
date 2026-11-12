import { useEffect, useId, useRef, type ReactNode } from "react";
import { X } from "lucide-react";

interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  headerActions?: ReactNode;
  variant?: "modal" | "drawer";
}

export function Dialog({
  open,
  onClose,
  title,
  children,
  headerActions,
  variant = "modal",
}: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (!open || !dialog) return;
    dialog.showModal();
    return () => {
      if (dialog.open) dialog.close();
    };
  }, [open]);

  if (!open) return null;

  const variantClass = variant === "drawer"
    ? "m-0 ml-auto h-dvh max-h-none w-full max-w-3xl rounded-none border-y-0 border-r-0"
    : "m-auto w-[min(640px,calc(100vw-2rem))] max-h-[85dvh] rounded-lg";

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      className={`${variantClass} overflow-y-auto border border-side-border bg-side-bg p-0 text-side-fg shadow-2xl`}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="sticky top-0 z-10 flex items-center justify-between gap-3 border-b border-side-border bg-side-bg/95 px-4 py-3 backdrop-blur">
        <h2 id={titleId} className="min-w-0 flex-1 truncate text-sm font-medium text-gray-100">{title}</h2>
        {headerActions}
        <button type="button" className="btn shrink-0 px-2" onClick={onClose} aria-label={`关闭${title}`}>
          <X size={15} aria-hidden="true" />
        </button>
      </div>
      <div className="p-4">{children}</div>
    </dialog>
  );
}
