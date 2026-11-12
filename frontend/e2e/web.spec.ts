import { readFileSync } from "node:fs";
import { expect, test, type Page, type Route } from "@playwright/test";

const version = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version as string;

const paper = {
  id: 1,
  dir_name: "example2024_sample",
  year: 2024,
  first_author: "example",
  keyword: "sample",
  title: "Sample paper",
  authors: ["Example Author"],
  journal: "Example Journal",
  doi: "10.1234/example.2024.01",
  arxiv_id: null,
  openalex_id: "https://openalex.org/W1",
  url: "https://example.org/paper",
  cited_by_count: 10,
  oa_status: "open",
  pub_type: "article",
  attachment_kind: null,
  parent_paper_id: null,
  concepts: [],
  abstract: "Abstract text",
  bibtex: null,
  pdf_path: null,
  parsed_path: "2024/paper/full.md",
  summary_path: null,
  pdf_pages: 1,
  pdf_size_bytes: 100,
  parsed_chars: 100,
  mineru_model: null,
  status_pdf: "ok",
  status_parsed: "ok",
  status_enrich: "ok",
  status_translated: "missing",
  status_summary: "ok",
  status_outline: "missing",
  status_embedded: "ok",
  pdf_title: null,
  pdf_authors: null,
  created_at: "2026-01-01 00:00:00",
  updated_at: "2026-01-01 00:00:00",
};

const task = {
  id: 100,
  kind: "translate",
  status: "ok",
  progress: 1,
  error: null,
  retries: 0,
  paper_id: 1,
  payload_json: "{}",
  log_path: null,
  created_at: "2026-01-01 00:00:00",
  started_at: "2026-01-01 00:00:01",
  finished_at: "2026-01-01 00:00:02",
};

interface ApiState {
  docText: string;
  docHash: string;
  saveDelay: number;
  saveBodies: { text: string; base_hash?: string | null }[];
  annotations: { id: number; paper_id: number; type: string; anchor: string; payload_json: string; source: string; created_at: string }[];
  taskPosts: number;
  secretsPresent: Record<string, boolean>;
  onPapers?: (route: Route, query: string, url: URL) => Promise<void>;
}

function respond(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function installApi(page: Page): Promise<ApiState> {
  const state: ApiState = {
    docText: "# Title\n\nalpha beta",
    docHash: "h1",
    saveDelay: 350,
    saveBodies: [],
    annotations: [],
    taskPosts: 0,
    secretsPresent: { fireworks_api_key: true },
  };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/papersys(?=\/|$)/, "") || "/";
    const method = request.method();
    if (path === "/api/health") return respond(route, { ok: true, service: "paper", version });
    if (path === "/api/events") return route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
    if (path === "/api/papers" && method === "GET") {
      const query = url.searchParams.get("q") || "";
      if (state.onPapers) return state.onPapers(route, query, url);
      return respond(route, { items: [paper], count: 1, limit: 50, offset: 0 });
    }
    if (path === "/api/papers/1" && method === "GET") return respond(route, paper);
    if (path === "/api/papers/1/translations") return respond(route, { items: [], count: 0 });
    if (path === "/api/papers/1/revisions") return respond(route, { items: [], count: 0 });
    if (path === "/api/papers/1/annotations" && method === "GET") return respond(route, { items: state.annotations, count: state.annotations.length });
    if (path === "/api/papers/1/annotations" && method === "POST") {
      const body = request.postDataJSON() as { type: string; anchor: string; quote: string; note: string; position: { start: number; end: number } };
      const annotation = {
        id: state.annotations.length + 1,
        paper_id: 1,
        type: body.type,
        anchor: body.anchor,
        payload_json: JSON.stringify({ quote: body.quote, note: body.note, position: body.position }),
        source: "web",
        created_at: "2026-01-01 00:00:00",
      };
      state.annotations = [annotation, ...state.annotations];
      return respond(route, { id: annotation.id }, 201);
    }
    if (path === "/api/papers/1/doc/en" && method === "GET") {
      return respond(route, { doc_type: "en", available: true, text: state.docText, source: "parsed", size: state.docText.length, hash: state.docHash });
    }
    if (path === "/api/papers/1/doc/en" && method === "PUT") {
      const body = request.postDataJSON() as { text: string; base_hash?: string | null };
      state.saveBodies.push(body);
      await new Promise((resolve) => setTimeout(resolve, state.saveDelay));
      state.docText = body.text;
      state.docHash = `h${state.saveBodies.length + 1}`;
      return respond(route, { saved: true, revision_id: state.saveBodies.length + 1, changed: true, hash: state.docHash, suggest_retranslate: true, reembed_task_id: null });
    }
    if (path === "/api/papers/1/retranslate" && method === "POST") {
      state.taskPosts += 1;
      return respond(route, { task_id: 100, reused: false });
    }
    if (path === "/api/papers/1/pipeline" && method === "POST") return respond(route, { task_id: 100, reused: false });
    if (path === "/api/semantic_search" && method === "POST") {
      return respond(route, {
        query: "decision",
        count: 1,
        stage: "rerank",
        items: [{ paper_id: 1, title: "Neuro paper", first_author: "author", year: 2026, chunk_index: 3, score: 0.87, text: "relevant fragment" }],
      });
    }
    if (path === "/api/tasks" && method === "GET") return respond(route, { items: [task], count: 1, total: 1, limit: 50, offset: 0 });
    if (path === "/api/tasks/100") return respond(route, { ...task, log_tail: "worker log" });
    if (path === "/api/inbox") return respond(route, { items: [], count: 0 });
    if (path === "/api/citations") {
      const nodes = [1, 2, 3].map((id) => ({ id: `p${id}`, label: `paper${id}`, title: `Paper ${id}`, year: 2026, author: "author", citations: 1, in_library: true, paper_id: id }));
      return respond(route, { nodes, edges: [{ id: "e1", source: "p1", target: "p2" }], stats: { library_nodes: 3, external_nodes: 0, edges: 1 } });
    }
    if (path === "/api/papers/1/images/fig.png") {
      return route.fulfill({ status: 200, contentType: "image/png", body: Buffer.from("iVBORw0KGgo=", "base64") });
    }
    if (path === "/api/settings") {
      return respond(route, { paths: {}, server: { host: "127.0.0.1", port: 5050, debug: false }, workers: {}, external: {}, mineru: {}, llm: { api_url: "", model: "test", temperature: 0 }, retrieval: {}, secrets: { present: state.secretsPresent } });
    }
    if (path === "/api/llm_usage") {
      const zero = { calls: 0, prompt_tokens: 0, completion_tokens: 0, cached_tokens: 0, prompt_chars: 0, errors: 0, cost_input: 0, cost_cached: 0, cost_output: 0, cost_total: 0 };
      return respond(route, { prices: { input_per_mtok: 0, cached_input_per_mtok: 0, output_per_mtok: 0 }, totals: zero, recent30d: zero, by_kind: [], by_model: [] });
    }
    return respond(route, { error: `unhandled ${method} ${path}`, code: 404 }, 404);
  });
  return state;
}

test("drafts survive tab switches and cancelled route navigation", async ({ page }) => {
  await installApi(page);
  await page.goto("./paper/1?tab=en");
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  const textarea = page.getByLabel("编辑 EN 文档");
  await textarea.fill("draft content");
  await page.getByRole("tab", { name: "信息" }).click();
  await page.getByRole("tab", { name: /原文/ }).click();
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  await expect(textarea).toHaveValue("draft content");
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("link", { name: "返回文献库" }).click();
  await expect(page).toHaveURL(/\/paper\/1\?tab=en$/);
});

test("text entered during save remains a dirty draft", async ({ page }) => {
  const state = await installApi(page);
  await page.goto("./paper/1?tab=en");
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  const textarea = page.getByLabel("编辑 EN 文档");
  await textarea.fill("first");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await textarea.fill("first second");
  await expect(page.getByText(/已保存（修订/)).toBeVisible();
  await expect(textarea).toHaveValue("first second");
  await expect(page.getByText("未保存", { exact: true })).toBeVisible();
  expect(state.saveBodies[0].text).toBe("first");
});

test("a slower old filter request cannot replace the latest result", async ({ page }) => {
  const state = await installApi(page);
  state.onPapers = async (route, query) => {
    if (query === "Sample") {
      await new Promise((resolve) => setTimeout(resolve, 500));
      return respond(route, { items: [paper], count: 1, limit: 50, offset: 0 });
    }
    if (query === "neuro") {
      await new Promise((resolve) => setTimeout(resolve, 50));
      return respond(route, { items: [{ ...paper, title: "Neuro paper" }], count: 1, limit: 50, offset: 0 });
    }
    return respond(route, { items: [], count: 0, limit: 50, offset: 0 });
  };
  await page.goto("./");
  const input = page.getByLabel("关键字");
  await input.fill("Sample");
  await page.waitForTimeout(300);
  await input.fill("neuro");
  await expect(page.getByRole("button", { name: "预览 Neuro paper" })).toBeVisible();
  await page.waitForTimeout(600);
  await expect(page.getByRole("button", { name: "预览 Neuro paper" })).toBeVisible();
  await expect(page.getByRole("button", { name: "预览 Sample paper" })).toHaveCount(0);
});

test("double clicking an action creates only one request", async ({ page }) => {
  const state = await installApi(page);
  await page.goto("./paper/1");
  await page.getByText("更多操作", { exact: true }).click();
  const button = page.getByRole("button", { name: "翻译全文" });
  await button.evaluate((element) => {
    element.click();
    element.click();
  });
  await expect.poll(() => state.taskPosts).toBe(1);
});

test("tables, details, subscripts and formulas render together", async ({ page }) => {
  const state = await installApi(page);
  state.docText = '<details>\n<summary>bar</summary>\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n</details>\n\nH<sub>2</sub>O\n\n$a < b$';
  await page.goto("./paper/1?tab=en");
  await page.getByText("bar", { exact: true }).click();
  await expect(page.getByRole("table")).toBeVisible();
  await expect(page.locator("sub")).toHaveText("2");
  await expect(page.locator(".katex")).toBeVisible();
  await expect(page.locator(".katex-error")).toHaveCount(0);
});

test("selection annotations restore highlights and support locating text", async ({ page }) => {
  const state = await installApi(page);
  state.docText = "alpha beta gamma";
  await page.goto("./paper/1?tab=en");
  await expect(page.locator(".markdown-body")).toContainText("alpha beta gamma");
  await page.evaluate(() => {
    const root = document.querySelector(".markdown-body")!;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const text = walker.nextNode() as Text;
    const range = document.createRange();
    range.setStart(text, 6);
    range.setEnd(text, 10);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
    root.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  });
  await page.getByLabel("批注备注").fill("术语");
  await page.getByRole("button", { name: "高亮批注" }).click();
  await expect(page.getByText("批注 (1)")).toBeVisible();
  const mark = page.locator("mark[data-annotation-id='1']");
  await expect(mark).toHaveText("beta");
  await page.getByRole("button", { name: "定位" }).click();
  await expect(mark).toHaveClass(/annotation-target/);
  const payload = JSON.parse(state.annotations[0].payload_json) as { position: { start: number; end: number } };
  expect(payload.position).toEqual({ start: 6, end: 10 });
});

test("semantic results link to the original document", async ({ page }) => {
  await installApi(page);
  await page.goto("./");
  await page.getByRole("tab", { name: "语义检索" }).click();
  await page.getByLabel("用一句话描述要找的内容").fill("decision");
  await page.getByRole("button", { name: "语义检索" }).click();
  await expect(page.getByText("Neuro paper")).toBeVisible();
  await expect(page.getByRole("link", { name: "阅读原文" })).toHaveAttribute("href", /\/paper\/1\?tab=en$/);
});

test("unknown routes show 404 and drawers close with Escape", async ({ page }) => {
  await installApi(page);
  await page.goto("./definitely-not-a-page");
  await expect(page.getByText("页面不存在")).toBeVisible();
  await page.goto("./");
  await page.getByRole("button", { name: "预览 Sample paper" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("the narrow layout keeps content usable and navigation reachable", async ({ page }) => {
  await installApi(page);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("./");
  await expect(page.getByRole("heading", { name: "文献库" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375);
  await page.getByRole("button", { name: "打开导航" }).click();
  await page.getByRole("link", { name: "任务", exact: true }).click();
  await expect(page).toHaveURL(/\/tasks$/);
  await expect(page.getByRole("heading", { name: "任务面板" })).toBeVisible();
});

test("the citation graph uses the local bundle and hides isolated nodes", async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem("paper.citations.comfort", "0"));
  await installApi(page);
  await page.goto("./citations");
  await expect(page.getByText(/当前显示 2 个节点、1 条边/)).toBeVisible();
  await expect(page.locator("canvas").first()).toBeVisible();
});

test("settings page shows retrieval availability and neutral optional keys", async ({ page }) => {
  const state = await installApi(page);
  state.secretsPresent = {
    fireworks_api_key: true,
    bp_embed_api_key: false,
    bp_rerank_api_key: false,
    ark_api_key: false,
    llm_api_key: false,
    retrieval_embed_available: true,
    retrieval_rerank_available: true,
  };
  await page.goto("./settings");
  await expect(page.getByText("嵌入可用", { exact: true })).toBeVisible();
  await expect(page.getByText("精排可用", { exact: true })).toBeVisible();
  await expect(page.getByRole("row", { name: /retrieval_embed_available/ })).toHaveCount(0);
  const bpRow = page.getByRole("row", { name: /bp_embed_api_key/ });
  await expect(bpRow.getByText("未设置")).toBeVisible();
  await expect(bpRow.getByText("未配置")).toHaveCount(0);
  const llmRow = page.getByRole("row", { name: /llm_api_key/ });
  await expect(llmRow.getByText("未配置")).toBeVisible();
});

test("configured base path serves deep routes, images and event streams", async ({ page }) => {
  const basePath = process.env.PLAYWRIGHT_BASE_PATH || "/";
  const state = await installApi(page);
  state.docText = "![figure](images/fig.png)";
  const direct = await page.request.get("./paper/1");
  expect(direct.status()).toBe(200);
  expect(await direct.text()).toContain('<div id="root"></div>');
  const events: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/events")) events.push(request.url());
  });
  await page.goto("./paper/1?tab=en");
  await expect(page.getByRole("img", { name: "figure" })).toHaveAttribute(
    "src", `${basePath}api/papers/1/images/fig.png`,
  );
  expect(events.every((url) => new URL(url).pathname.startsWith(`${basePath}api/events`))).toBe(true);
  expect(events.length).toBeGreaterThan(0);
});
