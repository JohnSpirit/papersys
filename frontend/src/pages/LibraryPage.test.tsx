import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Paper, type PapersResp, type SemanticSearchResp } from "@/lib/api";
import { LibraryPage } from "@/pages/LibraryPage";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, papers: vi.fn(), semanticSearch: vi.fn() } };
});

function paper(id: number, title: string): Paper {
  return {
    id,
    title,
    dir_name: `paper${id}_test`,
    year: 2026,
    first_author: "author",
    status_pdf: "ok",
    status_parsed: "ok",
    status_enrich: "ok",
    status_translated: "missing",
    status_summary: "ok",
    status_embedded: "ok",
  } as Paper;
}

function papers(items: Paper[], count = items.length): PapersResp {
  return { items, count, limit: 50, offset: 0 };
}

const semanticResponse: SemanticSearchResp = {
  query: "decision",
  count: 1,
  stage: "rerank",
  items: [{
    paper_id: 1,
    title: "Neuro paper",
    first_author: "author",
    year: 2026,
    chunk_index: 3,
    score: 0.87,
    text: "relevant fragment",
    char_start: 10,
    char_end: 30,
  }],
};

beforeEach(() => {
  vi.mocked(api.papers).mockResolvedValue(papers([]));
  vi.mocked(api.semanticSearch).mockResolvedValue(semanticResponse);
});

function renderLibrary() {
  const router = createMemoryRouter([
    { path: "/", element: <LibraryPage /> },
    { path: "/paper/:id", element: <div>paper detail</div> },
  ], { initialEntries: ["/"] });
  return render(<RouterProvider router={router} />);
}

describe("LibraryPage", () => {
  it("does not allow a slower old request to replace the latest query", async () => {
    vi.useFakeTimers();
    let resolveOld!: (value: PapersResp) => void;
    vi.mocked(api.papers).mockImplementation((params) => {
      if (params.q === "Sample") return new Promise((resolve) => { resolveOld = resolve; });
      if (params.q === "neuro") return Promise.resolve(papers([paper(1, "Neuro paper"), paper(2, "Second paper")], 2));
      return Promise.resolve(papers([]));
    });
    try {
      renderLibrary();
      await act(async () => { await vi.advanceTimersByTimeAsync(300); });
      const input = screen.getByLabelText("关键字");
      fireEvent.change(input, { target: { value: "Sample" } });
      await act(async () => { await vi.advanceTimersByTimeAsync(300); });
      fireEvent.change(input, { target: { value: "neuro" } });
      await act(async () => { await vi.advanceTimersByTimeAsync(300); });
      expect(screen.getAllByText("Neuro paper").length).toBeGreaterThan(0);
      await act(async () => { resolveOld(papers([paper(3, "Sample")], 1)); });
      expect(input).toHaveValue("neuro");
      expect(screen.getAllByText("Neuro paper").length).toBeGreaterThan(0);
      expect(screen.queryByText("Sample")).toBeNull();
      expect(screen.getByText("共 2 篇")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows semantic results with links to the original document", async () => {
    const user = userEvent.setup();
    renderLibrary();
    await user.click(screen.getByRole("tab", { name: "语义检索" }));
    await user.type(screen.getByLabelText("用一句话描述要找的内容"), "decision");
    await user.click(screen.getByRole("button", { name: "语义检索" }));
    expect(await screen.findByText("Neuro paper")).toBeInTheDocument();
    expect(screen.getByText("relevant fragment")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "阅读原文" })).toHaveAttribute("href", "/paper/1?tab=en");
    expect(screen.getByText(/相关度 87.0%/)).toBeInTheDocument();
  });

  it("passes the next page offset and exposes all status options", async () => {
    vi.useFakeTimers();
    vi.mocked(api.papers).mockImplementation((params) => Promise.resolve({
      items: [paper(Number(params.offset) + 1, `Paper offset ${params.offset}`)],
      count: 120,
      limit: 50,
      offset: Number(params.offset),
    }));
    try {
      renderLibrary();
      await act(async () => { await vi.advanceTimersByTimeAsync(300); });
      expect(screen.getByText("共 120 篇")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "下一页" }));
      await act(async () => { await vi.advanceTimersByTimeAsync(300); });
      expect(vi.mocked(api.papers).mock.calls.at(-1)?.[0]?.offset).toBe(50);
      const status = screen.getByLabelText("翻译状态");
      expect(within(status).getByRole("option", { name: "部分完成" })).toBeInTheDocument();
      expect(within(status).getByRole("option", { name: "已跳过" })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});
