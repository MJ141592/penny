"use client";

import { useEffect, useState } from "react";

/**
 * Pairing screen. GOWA mints a new QR roughly every 30s, so the image is
 * re-requested on a timer with a cache-busting key until login succeeds.
 */
export default function LoginPanel({ onLinked }: { onLinked: () => void }) {
  const [nonce, setNonce] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const rotate = setInterval(() => setNonce((n) => n + 1), 25_000);
    return () => clearInterval(rotate);
  }, []);

  // Poll connection state so the UI advances the moment the phone confirms.
  useEffect(() => {
    const poll = setInterval(async () => {
      try {
        const res = await fetch("/api/gowa/app/status");
        const body = await res.json();
        if (body?.results?.is_logged_in) onLinked();
      } catch {
        /* engine may be restarting */
      }
    }, 2_000);
    return () => clearInterval(poll);
  }, [onLinked]);

  return (
    <div className="flex h-full items-center justify-center bg-[var(--bg-sunken)] p-6">
      <div className="w-full max-w-md rounded-2xl border border-[var(--border)] bg-[var(--bg-raised)] p-8 text-center shadow-sm">
        <h1 className="text-xl font-semibold">Link your WhatsApp Business account</h1>
        <p className="mt-2 text-sm text-[var(--text-dim)]">
          On your phone: <strong>Settings → Linked devices → Link a device</strong>,
          then scan this code.
        </p>

        <div className="mx-auto mt-6 flex h-64 w-64 items-center justify-center rounded-xl bg-white p-3">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            key={nonce}
            src={`/api/qr?n=${nonce}`}
            alt="WhatsApp pairing QR code"
            className="h-full w-full object-contain"
            onError={() => setError("Could not load the QR code.")}
            onLoad={() => setError(null)}
          />
        </div>

        {error ? (
          <p className="mt-4 text-sm text-red-500">
            {error} Check that the engine is running (<code>./engine/start.sh</code>).
          </p>
        ) : (
          <p className="mt-4 text-xs text-[var(--text-dim)]">
            The code refreshes automatically. This links as a companion device —
            your phone stays connected.
          </p>
        )}
      </div>
    </div>
  );
}
