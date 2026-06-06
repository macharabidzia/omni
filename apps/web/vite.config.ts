import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const gatewayHost = process.env.GATEWAY_HOST ?? "127.0.0.1";
const gatewayPort = process.env.GATEWAY_PORT ?? "8000";
const gatewayHttpTarget =
  process.env.VITE_DEV_PROXY_HTTP_TARGET ?? `http://${gatewayHost}:${gatewayPort}`;
const livekitTarget = normalizeProxyTarget(
  process.env.VITE_DEV_PROXY_LIVEKIT_TARGET ?? process.env.LIVEKIT_URL ?? "ws://185.62.58.164:7880",
);
const vendorChunks: Array<[pkgPath: string, chunkName: string]> = [
  ["/node_modules/react/", "react"],
  ["/node_modules/react-dom/", "react"],
  ["/node_modules/livekit-client/", "livekit-client"],
  ["/node_modules/@livekit/protocol/", "livekit-protocol"],
  ["/node_modules/@livekit/mutex/", "livekit-protocol"],
  ["/node_modules/webrtc-adapter/", "webrtc-adapter"],
  ["/node_modules/jose/", "jose"],
];

function normalizeProxyTarget(value: string): string {
  if (value.startsWith("ws://")) {
    return `http://${value.slice("ws://".length)}`;
  }
  if (value.startsWith("wss://")) {
    return `https://${value.slice("wss://".length)}`;
  }
  return value;
}

export default defineConfig({
  plugins: [react()],
  build: {
    chunkSizeWarningLimit: 550,
    rollupOptions: {
      output: {
        manualChunks(id) {
          for (const [pkgPath, chunkName] of vendorChunks) {
            if (id.includes(pkgPath)) {
              return chunkName;
            }
          }
          if (id.includes("/node_modules/")) {
            return "vendor";
          }
          return undefined;
        },
      },
    },
  },
  server: {
    host: "0.0.0.0",
    port: Number(process.env.WEB_PORT ?? 3000),
    proxy: {
      "/ready": {
        target: gatewayHttpTarget,
        changeOrigin: true,
      },
      "/livekit": {
        target: gatewayHttpTarget,
        changeOrigin: true,
      },
      "/livekit-proxy": {
        target: livekitTarget,
        changeOrigin: true,
        ws: true,
        rewrite: (path) => path.replace(/^\/livekit-proxy/, ""),
      },
    },
  },
});
