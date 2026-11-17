import { expect, test } from "@playwright/test";

test("foundation landing page renders core heading", async ({ page }) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Document SaaS — Fondasi Aplikasi" }),
  ).toBeVisible();
});
