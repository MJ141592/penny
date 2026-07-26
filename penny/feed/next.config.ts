import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Lockfiles sit above this directory; pin the root so Turbopack doesn't
  // infer the home directory.
  turbopack: { root: path.join(__dirname) },
};

export default nextConfig;
