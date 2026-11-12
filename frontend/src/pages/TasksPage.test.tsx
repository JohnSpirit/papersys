import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Task, type TasksResp } from "@/lib/api";
import { TasksPage } from "@/pages/TasksPage";
import { ToastProvider } from "@/components/Toast";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, tasks: vi.fn(), inbox: vi.fn(), task: vi.fn(), taskRetry: vi.fn() },
  };
});

vi.mock("@/lib/sse", () => ({
  useEventStream: () => ({ events: [], connected: true }),
}));

function task(id: number, status: Task["status"] = "error"): Task {
  return {
    id,
    kind: "translate",
    status,
    progress: status === "ok" ? 1 : 0.5,
    error: status === "error" ? "full worker error" : null,
    retries: 1,
    paper_id: 1,
    payload_json: JSON.stringify({ paper_id: 1, force: false }),
    payload: { paper_id: 1, force: false },
    log_path: null,
    log_tail: "full worker log",
    created_by_user_id: null,
    execution_subject: "system",
    required_permission: null,
    created_at: "2026-01-01 01:02:03",
    started_at: "2026-01-01 01:03:03",
    finished_at: "2026-01-01 01:04:03",
  };
}

function response(items: Task[], offset = 0): TasksResp {
  return { items, count: items.length, total: 1392, limit: 50, offset };
}

beforeEach(() => {
  vi.mocked(api.tasks).mockImplementation((params = {}) => Promise.resolve(response([task(Number(params.offset) + 1)], Number(params.offset))));
  vi.mocked(api.inbox).mockResolvedValue({ items: [], count: 0 });
  vi.mocked(api.task).mockImplementation((id) => Promise.resolve(task(id)));
  vi.mocked(api.taskRetry).mockResolvedValue({ new_task_id: 999 });
});

function renderTasks() {
  const router = createMemoryRouter([
    { path: "/tasks", element: <ToastProvider><TasksPage /></ToastProvider> },
    { path: "/paper/:id", element: <div>paper</div> },
  ], { initialEntries: ["/tasks"] });
  return render(<RouterProvider router={router} />);
}

describe("TasksPage", () => {
  it("shows the real total and requests the next page", async () => {
    const user = userEvent.setup();
    renderTasks();
    expect((await screen.findAllByText(/共 1392 个任务/)).length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(vi.mocked(api.tasks).mock.calls.at(-1)?.[0]?.offset).toBe(50));
    expect(await screen.findByText("51–100 / 共 1392 个任务")).toBeInTheDocument();
  });

  it("opens full task details and retries a cancelled task", async () => {
    const user = userEvent.setup();
    vi.mocked(api.tasks).mockResolvedValue(response([task(7, "cancelled")]));
    vi.mocked(api.task).mockResolvedValue(task(7, "cancelled"));
    renderTasks();
    await user.click((await screen.findAllByRole("button", { name: "详情" }))[0]);
    expect(await screen.findByText("full worker log")).toBeInTheDocument();
    expect(screen.getByText("任务参数")).toBeInTheDocument();
    expect(screen.getByText("开始时间")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "创建重试任务" }));
    await waitFor(() => expect(api.taskRetry).toHaveBeenCalledWith(7));
  });
});
