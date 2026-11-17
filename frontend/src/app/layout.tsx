import type { Metadata } from "next";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "Document SaaS — Fondasi Aplikasi",
  description:
    "Platform penyusunan dokumen formal berbasis sumber dengan peninjauan dan persetujuan manusia.",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="id">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
