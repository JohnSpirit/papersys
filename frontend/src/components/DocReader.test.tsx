import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, type DocResp } from "@/lib/api";
import { DocReader } from "@/components/DocReader";
import { ToastProvider } from "@/components/Toast";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      doc: vi.fn(),
      docSave: vi.fn(),
      revisions: vi.fn(),
      revision: vi.fn(),
      revisionRestore: vi.fn(),
      annotations: vi.fn(),
      annotationCreate: vi.fn(),
      annotationDelete: vi.fn(),
    },
  };
});

function document(text: string, hash = "h1"): DocResp {
  return { doc_type: "en", available: true, text, source: "parsed", size: text.length, hash };
}

const saveResult = {
  saved: true,
  revision_id: 2,
  changed: true,
  hash: "h2",
  suggest_retranslate: true,
  reembed_task_id: null,
};

const revision = {
  id: 1,
  paper_id: 1,
  doc_type: "en" as const,
  content_hash: "old-hash",
  source: "web_edit",
  size_bytes: 16,
  payload_path: null,
  created_by_user_id: null,
  ts: "2026-01-01 00:00:00",
};

beforeEach(() => {
  vi.mocked(api.doc).mockResolvedValue(document("alpha"));
  vi.mocked(api.docSave).mockResolvedValue(saveResult);
  vi.mocked(api.revisions).mockResolvedValue({ items: [revision], count: 1 });
  vi.mocked(api.revision).mockResolvedValue({ ...revision, content: "snapshot content" });
  vi.mocked(api.annotations).mockResolvedValue({ items: [], count: 0 });
});

function renderReader(draft?: { text: string; dirty: boolean; baseHash: string | null }) {
  return render(
    <ToastProvider>
      <DocReader paperId={1} docType="en" draft={draft} />
    </ToastProvider>,
  );
}

describe("DocReader", () => {
  it("restores a dirty draft instead of replacing it with the server document", async () => {
    const user = userEvent.setup();
    renderReader({ text: "dirty draft", dirty: true, baseHash: "h0" });
    await user.click(await screen.findByRole("button", { name: "编辑" }));
    expect(await screen.findByLabelText("编辑 EN 文档")).toHaveValue("dirty draft");
    expect(screen.getByText("未保存")).toBeInTheDocument();
  });

  it("keeps text entered while a save is in flight", async () => {
    const user = userEvent.setup();
    let resolveSave!: (value: typeof saveResult) => void;
    vi.mocked(api.docSave).mockReturnValue(new Promise((resolve) => { resolveSave = resolve; }));
    renderReader();
    await user.click(await screen.findByRole("button", { name: "编辑" }));
    const textarea = await screen.findByLabelText("编辑 EN 文档");
    await user.clear(textarea);
    await user.type(textarea, "first");
    await user.click(screen.getByRole("button", { name: "保存" }));
    await user.type(textarea, " second");
    await act(async () => { resolveSave(saveResult); });
    await waitFor(() => expect(textarea).toHaveValue("first second"));
    expect(screen.getByText("未保存")).toBeInTheDocument();
    expect(api.docSave).toHaveBeenCalledWith(1, "en", "first", "h1");
  });

  it("shows a conflict and only overwrites with the current server hash", async () => {
    const user = userEvent.setup();
    vi.mocked(api.docSave)
      .mockRejectedValueOnce(new ApiError("document changed", 409, { current_hash: "h2" }))
      .mockResolvedValue(saveResult);
    renderReader();
    await user.click(await screen.findByRole("button", { name: "编辑" }));
    const textarea = await screen.findByLabelText("编辑 EN 文档");
    await user.type(textarea, " draft");
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText(/服务器内容已变化/)).toBeInTheDocument();
    expect(textarea).toHaveValue("alpha draft");
    await user.click(screen.getByRole("button", { name: "使用草稿覆盖" }));
    await waitFor(() => expect(api.docSave).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.docSave).mock.calls[1]).toEqual([1, "en", "alpha draft", "h2"]);
  });

  it("loads a revision snapshot before offering rollback", async () => {
    const user = userEvent.setup();
    renderReader();
    await user.click(await screen.findByRole("button", { name: /历史 \(1\)/ }));
    await user.click(screen.getByRole("button", { name: "预览" }));
    expect(await screen.findByText("snapshot content")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "回滚到此版本" })).toBeInTheDocument();
    expect(api.revisionRestore).not.toHaveBeenCalled();
  });
});
