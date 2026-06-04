import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const gatewayHost = process.env.GATEWAY_HOST ?? "127.0.0.1";
const gatewayPort = process.env.GATEWAY_PORT ?? "8000";
const gatewayHttpTarget =
  process.env.VITE_DEV_PROXY_HTTP_TARGET ?? `http://${gatewayHost}:${gatewayPort}`;
const gatewayWsTarget =
  process.env.VITE_DEV_PROXY_WS_TARGET ?? `ws://${gatewayHost}:${gatewayPort}`;

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: Number(process.env.WEB_PORT ?? 3000),
    proxy: {
      "/ready": {
        target: gatewayHttpTarget,
        changeOrigin: true,
      },
      "/ws": {
        target: gatewayWsTarget,
        changeOrigin: true,
        ws: true,
      },
    },
  },
});
