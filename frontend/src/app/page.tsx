import Link from "next/link";

import { getApiBaseUrl } from "@/lib/config";

export default function HomePage() {
  const apiBaseUrl = getApiBaseUrl();

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col justify-center px-6 py-16">
      <section
        aria-labelledby="foundation-heading"
        className="rounded-lg border border-slate-200 bg-white p-8 shadow-sm"
      >
        <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">
          Milestone M2 — Autentikasi &amp; Fondasi
        </p>
        <h1
          id="foundation-heading"
          className="mt-2 text-2xl font-semibold tracking-tight text-slate-900"
        >
          Document SaaS — Fondasi Aplikasi
        </h1>
        <p className="mt-3 text-sm leading-6 text-slate-600">
          Antarmuka web statis siap terhubung ke API FastAPI sebagai otoritas utama aplikasi.
          Seluruh draf dokumen wajib ditinjau dan disetujui pengguna sebelum ekspor DOCX.
        </p>

        <dl className="mt-6 grid gap-4 border-t border-slate-100 pt-6 text-sm sm:grid-cols-2">
          <div>
            <dt className="font-medium text-slate-500">Status Layanan</dt>
            <dd className="mt-1 font-mono text-slate-900">Fondasi aktif (Static Export)</dd>
          </div>
          <div>
            <dt className="font-medium text-slate-500">Basis API</dt>
            <dd className="mt-1 truncate font-mono text-slate-900">{apiBaseUrl}</dd>
          </div>
        </dl>

        <div className="mt-6 flex flex-wrap gap-3 border-t border-slate-100 pt-6">
          <Link
            href="/login"
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800"
          >
            Masuk
          </Link>
          <Link
            href="/register"
            className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            Daftar Akun
          </Link>
        </div>
      </section>
    </main>
  );
}
