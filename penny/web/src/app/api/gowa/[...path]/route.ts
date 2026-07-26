import { NextRequest, NextResponse } from "next/server";
import { gowa, GowaError } from "@/lib/gowa";

export const dynamic = "force-dynamic";

type Ctx = RouteContext<"/api/gowa/[...path]">;

async function forward(req: NextRequest, ctx: Ctx, method: "GET" | "POST") {
  const { path } = await ctx.params;
  const search = req.nextUrl.search;

  // Segments arrive already decoded by Next; re-encode so JIDs like
  // "1203...@g.us" survive as a single path segment.
  const target = `/${path.map(encodeURIComponent).join("/")}${search}`;

  const init: RequestInit = { method };
  if (method === "POST") {
    init.body = await req.text();
  }

  try {
    const data = await gowa(target, init);
    return NextResponse.json(data);
  } catch (err) {
    if (err instanceof GowaError) {
      return NextResponse.json(
        { error: err.message, code: "GOWA_ERROR" },
        { status: err.status },
      );
    }
    return NextResponse.json(
      { error: "Engine unreachable — is engine/start.sh running?", code: "ENGINE_DOWN" },
      { status: 502 },
    );
  }
}

export const GET = (req: NextRequest, ctx: Ctx) => forward(req, ctx, "GET");
export const POST = (req: NextRequest, ctx: Ctx) => forward(req, ctx, "POST");
