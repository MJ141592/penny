import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Several lockfiles sit above this directory; without this Turbopack infers
  // the home directory as the workspace root.
  turbopack: {
    root: path.join(__dirname),
  },
};

export default nextConfig;
