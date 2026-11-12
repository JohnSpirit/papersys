import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MarkdownView } from "@/components/MarkdownView";
import { splitDetailsBlocks } from "@/lib/markdown-details";

describe("MarkdownView", () => {
  it("renders pipe tables as actual tables", () => {
    const { container } = render(<MarkdownView paperId={1} text={"| A | B |\n| --- | --- |\n| 1 | 2 |"} />);
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "A" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "1" })).toBeInTheDocument();
    expect(container.textContent).not.toContain("| --- |");
  });

  it("renders allowed HTML and removes unsafe content", () => {
    const { container } = render(
      <MarkdownView
        paperId={1}
        text={'<table><tr><th>A</th></tr><tr><td>1</td></tr></table><p>H<sub>2</sub>O</p><script>alert(1)</script><p onclick="alert(1)">safe</p><a href="javascript:alert(1)">bad</a>'}
      />,
    );
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(container.querySelector("sub")).toHaveTextContent("2");
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("p[onclick]")).toBeNull();
    expect(screen.getByText("bad")).not.toHaveAttribute("href", "javascript:alert(1)");
  });

  it("parses pipe tables inside MinerU details blocks", () => {
    const text = '<details>\n<summary>bar</summary>\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n</details>';
    const { container } = render(<MarkdownView paperId={1} text={text} />);
    expect(container.querySelector("details")).toBeInTheDocument();
    expect(screen.getByText("bar")).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(splitDetailsBlocks(text)[0].kind).toBe("details");
  });

  it("keeps formulas containing angle brackets working", () => {
    const { container } = render(<MarkdownView paperId={1} text="$a < b$" />);
    expect(container.querySelector(".katex")).toBeInTheDocument();
    expect(container.querySelector(".katex-error")).toBeNull();
  });

  it("highlights and focuses annotated text", () => {
    const { container } = render(
      <MarkdownView
        paperId={1}
        text="alpha beta gamma"
        focusAnnotationId={7}
        annotations={[{ id: 7, quote: "beta", note: "术语", position: { start: 6, end: 10 } }]}
      />,
    );
    const mark = container.querySelector("mark[data-annotation-id='7']");
    expect(mark).toHaveTextContent("beta");
    expect(mark).toHaveClass("annotation-target");
    expect(mark).toHaveAttribute("title", "术语");
  });
});
