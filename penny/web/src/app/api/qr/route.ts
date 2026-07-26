import { NextResponse } from "next/server";
import { gowa, GowaError } from "@/lib/gowa";

export const dynamic = "force-dynamic";

const BASE = process.env.GOWA_URL ?? "http://localhost:3000";
const USER = process.env.GOWA_USER ?? "penny";
const PASS = process.env.GOWA_PASS ?? "";
const authHeader = `Basic ${Buffer.from(`${USER}:${PASS}`).toString("base64")}`;

type LoginResults = { qr_link?: string; qr_duration?: number };

/**
 * Requests a fresh pairing QR from the engine and streams the PNG back.
 * The image itself sits behind GOWA's basic auth, so the browser can't
 * fetch `qr_link` directly — this route re-fetches it with credentials.
 */
export async function GET() {
  try {
    const res = await gowa<{ results?: LoginResults }>("/app/login");
    const link = res.results?.qr_link;

    if (!link) {
      return NextResponse.json(
        { error: "No QR returned — the account may already be linked." },
        { status: 409 },
      );
    }

    // Only the path is trusted; the host is always our configured engine.
    const path = new URL(link).pathname;
    const img = await fetch(`${BASE}${path}`, {
      headers: { Authorization: authHeader },
      cache: "no-store",
    });

    if (!img.ok) {
      return NextResponse.json(
        { error: `Could not load QR image (${img.status})` },
        { status: 502 },
      );
    }

    return new NextResponse(img.body, {
      headers: {
        "Content-Type": img.headers.get("content-type") ?? "image/png",
        "Cache-Control": "no-store",
        "X-QR-Duration": String(res.results?.qr_duration ?? 30),
      },
    });
  } catch (err) {
    const status = err instanceof GowaError ? err.status : 502;
    const message =
      err instanceof GowaError
        ? err.message
        : "Engine unreachable — is engine/start.sh running?";
    return NextResponse.json({ error: message }, { status });
  }
}
