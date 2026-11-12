import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ExternalLink } from "lucide-react";
import type { Paper } from "@/lib/api";
import { PaperDetail } from "@/components/PaperDetail";
import { Dialog } from "@/components/Dialog";

interface Props {
  paper: Paper | null;
  onClose: () => void;
}

export function PaperDrawer({ paper, onClose }: Props) {
  const navigate = useNavigate();
  const [dirtyState, setDirtyState] = useState<{ paperId: number; dirty: boolean } | null>(null);
  const dirty = !!paper && dirtyState?.paperId === paper.id && dirtyState.dirty;

  function allowClose() {
    return !dirty || window.confirm("当前论文有未保存的文档修改。确定关闭并放弃这些修改吗？");
  }

  function requestClose() {
    if (!allowClose()) return;
    setDirtyState(null);
    onClose();
  }

  function openDetails() {
    if (!paper || !allowClose()) return;
    const paperId = paper.id;
    setDirtyState(null);
    onClose();
    setTimeout(() => { void navigate(`/paper/${paperId}`); }, 0);
  }

  return (
    <Dialog
      open={paper !== null}
      onClose={requestClose}
      title={paper?.title || paper?.dir_name || "论文详情"}
      variant="drawer"
      headerActions={(
        <button type="button" className="inline-flex items-center gap-1 text-xs text-blue-300 hover:text-blue-200" onClick={openDetails}>
          <ExternalLink size={12} aria-hidden="true" /> 详情页
        </button>
      )}
    >
      {paper && (
        <PaperDetail
          key={paper.id}
          paper={paper}
          onDirtyChange={(value) => setDirtyState({ paperId: paper.id, dirty: value })}
        />
      )}
    </Dialog>
  );
}
