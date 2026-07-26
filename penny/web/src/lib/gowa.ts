import "server-only";

const BASE = process.env.GOWA_URL ?? "http://localhost:3000";
const USER = process.env.GOWA_USER ?? "penny";
const PASS = process.env.GOWA_PASS ?? "";

const authHeader = `Basic ${Buffer.from(`${USER}:${PASS}`).toString("base64")}`;

export class GowaError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/** GOWA v9 is multi-account: every call is scoped to a registered device slot. */
export const DEVICE_ID = process.env.GOWA_DEVICE_ID ?? "penny";

const cache = globalThis as unknown as { __pennyDevice?: Promise<void> };

/**
 * Registers our device slot once per process. GOWA returns an error if the
 * device already exists, which is fine — we only need it to be present.
 */
function ensureDevice(): Promise<void> {
  cache.__pennyDevice ??= (async () => {
    try {
      const list = await raw<{ results: Array<{ id: string }> | null }>("/devices");
      if (list.results?.some((d) => d.id === DEVICE_ID)) return;
      await raw("/devices", {
        method: "POST",
        body: JSON.stringify({ device_id: DEVICE_ID }),
      });
    } catch {
      // Leave provisioning to the next call rather than wedging the process.
      cache.__pennyDevice = undefined;
    }
  })();
  return cache.__pennyDevice;
}

/** Bare request with auth but no device provisioning — avoids recursion. */
async function raw<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      Authorization: authHeader,
      "Content-Type": "application/json",
      "X-Device-Id": DEVICE_ID,
      ...(init.headers ?? {}),
    },
    cache: "no-store",
  });

  const text = await res.text();
  let body: unknown;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }

  if (!res.ok) {
    const msg =
      (body as { message?: string })?.message ??
      (typeof body === "string" ? body : res.statusText);
    throw new GowaError(msg || "GOWA request failed", res.status);
  }
  return body as T;
}

/**
 * Calls the GOWA engine. Credentials live here on the server only — the browser
 * reaches GOWA exclusively through /api/gowa/* so they never ship to the client.
 */
export async function gowa<T = unknown>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  // /health and /devices are device-agnostic; everything else needs the slot.
  if (!path.startsWith("/health") && !path.startsWith("/devices")) {
    await ensureDevice();
  }
  return raw<T>(path, init);
}
