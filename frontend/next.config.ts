import type { NextConfig } from "next";

// O frontend nunca recebe chaves de API: todas as chamadas de IA acontecem no backend.
const API_URL = process.env.API_URL || "http://127.0.0.1:8000";

const config: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  compress: false, // não comprimir: o proxy precisa repassar o SSE de progresso sem buffer
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_URL}/api/:path*` }];
  },
  experimental: { proxyTimeout: 600_000 },
};

export default config;
