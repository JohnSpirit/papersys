import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { api, errMsg, type Paper } from "@/lib/api";
import { PaperDetail } from "@/components/PaperDetail";
import { ErrorState, LoadingState } from "@/components/Feedback";

type RequestedTab = "info" | "en" | "zh" | "summary";

export function PaperDetailPage() {
  const { id = "" } = useParams<{ id: string }>();
  const [searchParams] = useSearchParams();
  const [reloadKey, setReloadKey] = useState(0);
  const [result, setResult] = useState<{ key: string; paper?: Paper; error?: string } | null>(null);
  const validId = /^\d+$/.test(id) && Number(id) > 0;
  const requestKey = `${id}:${reloadKey}`;
  const loading = validId && result?.key !== requestKey;
  const requestedTab = searchParams.get("tab");
  const initialTab: RequestedTab = ["en", "zh", "summary"].includes(requestedTab || "")
    ? requestedTab as RequestedTab
    : "info";

  useEffect(() => {
    if (!validId) return;
    const controller = new AbortController();
    api.paper(Number(id), controller.signal).then((paper) => {
      if (!controller.signal.aborted) setResult({ key: requestKey, paper });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setResult({ key: requestKey, error: errMsg(error) });
    });
    return () => controller.abort();
  }, [id, validId, requestKey]);

  if (!validId) {
    return <div className="p-4 sm:p-6"><ErrorState message="论文编号无效，请从文献库重新打开论文。" /></div>;
  }
  if (loading) return <div className="p-4 sm:p-6"><LoadingState label="正在读取论文…" /></div>;
  if (result?.error) {
    return <div className="p-4 sm:p-6"><ErrorState message={result.error} onRetry={() => setReloadKey((value) => value + 1)} /></div>;
  }
  if (!result?.paper) {
    return <div className="p-4 sm:p-6"><ErrorState message="论文不存在。" /></div>;
  }

  return (
    <div className="p-4 sm:p-6">
      <Link to="/" className="mb-4 inline-flex items-center gap-1 text-sm text-gray-300 hover:text-gray-100">
        <ArrowLeft size={14} aria-hidden="true" /> 返回文献库
      </Link>
      <PaperDetail key={`${result.paper.id}:${initialTab}`} paper={result.paper} initialTab={initialTab} />
    </div>
  );
}
