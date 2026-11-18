"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";

import { ApiClientError, AuthTokenData, registerUser } from "@/lib/auth";

export default function RegisterPage() {
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [registeredSession, setRegisteredSession] = useState<AuthTokenData | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMessage(null);
    setSubmitting(true);

    try {
      const data = await registerUser({
        display_name: displayName,
        email,
        password,
      });
      setRegisteredSession(data);
    } catch (error) {
      if (error instanceof ApiClientError) {
        setErrorMessage(error.message);
      } else {
        setErrorMessage("Tidak dapat terhubung ke server pendaftaran.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6 py-12">
      <section
        aria-labelledby="register-heading"
        className="rounded-lg border border-slate-200 bg-white p-8 shadow-sm"
      >
        <h1 id="register-heading" className="text-2xl font-semibold tracking-tight text-slate-900">
          Daftar Akun Baru
        </h1>
        <p className="mt-2 text-sm text-slate-600">
          Buat akun untuk mulai menyusun dan meninjau dokumen formal.
        </p>

        {errorMessage ? (
          <div
            role="alert"
            className="mt-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
          >
            {errorMessage}
          </div>
        ) : null}

        {registeredSession ? (
          <div
            role="status"
            className="mt-4 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800"
          >
            Akun {registeredSession.user.email} berhasil dibuat. Periksa email Anda untuk
            verifikasi.
          </div>
        ) : null}

        <form onSubmit={handleSubmit} className="mt-6 space-y-4">
          <div>
            <label htmlFor="register-name" className="block text-sm font-medium text-slate-700">
              Nama Tampilan
            </label>
            <input
              id="register-name"
              name="display_name"
              type="text"
              required
              autoComplete="name"
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 focus:border-slate-900 focus:outline-none"
            />
          </div>

          <div>
            <label htmlFor="register-email" className="block text-sm font-medium text-slate-700">
              Email
            </label>
            <input
              id="register-email"
              name="email"
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 focus:border-slate-900 focus:outline-none"
            />
          </div>

          <div>
            <label htmlFor="register-password" className="block text-sm font-medium text-slate-700">
              Kata Sandi
            </label>
            <input
              id="register-password"
              name="password"
              type="password"
              minLength={8}
              required
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 focus:border-slate-900 focus:outline-none"
            />
          </div>

          <button
            type="submit"
            disabled={submitting}
            className="w-full rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {submitting ? "Mendaftar..." : "Daftar"}
          </button>
        </form>

        <p className="mt-6 text-center text-sm text-slate-600">
          Sudah memiliki akun?{" "}
          <Link href="/login" className="font-medium text-slate-900 underline">
            Masuk di sini
          </Link>
        </p>
      </section>
    </main>
  );
}

