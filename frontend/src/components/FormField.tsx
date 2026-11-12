import type { ReactNode } from "react";

interface FormFieldProps {
  label: string;
  children: ReactNode;
  className?: string;
  group?: boolean;
}

export function FormField({ label, children, className = "", group = false }: FormFieldProps) {
  const classes = `flex flex-col gap-1 ${className}`;
  if (group) {
    return (
      <fieldset className={classes}>
        <legend className="text-xs text-gray-400">{label}</legend>
        {children}
      </fieldset>
    );
  }
  return (
    <label className={classes}>
      <span className="text-xs text-gray-400">{label}</span>
      {children}
    </label>
  );
}
