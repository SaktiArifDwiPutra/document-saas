import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import HomePage from "@/app/page";

describe("HomePage foundation", () => {
  it("renders Indonesian-first foundation heading and API base endpoint info", () => {
    render(<HomePage />);

    expect(
      screen.getByRole("heading", { name: "Document SaaS — Fondasi Aplikasi" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Fondasi aktif \(Static Export\)/i)).toBeInTheDocument();
    expect(screen.getByText("http://localhost:8000/api/v1")).toBeInTheDocument();
  });
});
