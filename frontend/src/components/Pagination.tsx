import { ChevronLeft, ChevronRight } from "lucide-react";

interface PaginationProps {
  page: number;
  pageSize: number;
  total: number;
  onChange: (page: number) => void;
  itemLabel?: string;
}

export function Pagination({ page, pageSize, total, onChange, itemLabel = "条" }: PaginationProps) {
  if (total === 0) return null;
  const totalPages = Math.ceil(total / pageSize);
  const from = page * pageSize + 1;
  const to = Math.min((page + 1) * pageSize, total);
  return (
    <nav className="flex flex-wrap items-center justify-between gap-2 text-xs text-gray-400" aria-label="分页">
      <span>{from}–{to} / 共 {total} {itemLabel}</span>
      {totalPages > 1 && (
        <div className="flex items-center gap-1">
          <button type="button" className="btn text-xs" disabled={page === 0} onClick={() => onChange(page - 1)} aria-label="上一页">
            <ChevronLeft size={14} aria-hidden="true" /> 上一页
          </button>
          <span className="px-2" aria-current="page">{page + 1} / {totalPages}</span>
          <button type="button" className="btn text-xs" disabled={page + 1 >= totalPages} onClick={() => onChange(page + 1)} aria-label="下一页">
            下一页 <ChevronRight size={14} aria-hidden="true" />
          </button>
        </div>
      )}
    </nav>
  );
}
