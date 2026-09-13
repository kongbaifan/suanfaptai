import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { resolve } from "node:path";

// 后端开发服务器默认端口。README、CLAUDE.md、docs/接口文档.md 与
// docs/项目文档.md 一律以 8010 为默认（uvicorn app.main:app --port 8010），
// 此处与之一致，避免"照文档起后端、前端代理却打不通"。
const BACKEND_ORIGIN = "http://127.0.0.1:8010";

export default defineConfig({
  base: "/static/",
  plugins: [vue()],
  resolve: { alias: { "@": resolve(__dirname, "src") } },
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": BACKEND_ORIGIN,
      "/health": BACKEND_ORIGIN,
      "/ws": { target: BACKEND_ORIGIN.replace(/^http/, "ws"), ws: true },
    },
  },
  build: {
    outDir: resolve(__dirname, "../app/static"),
    emptyOutDir: true,
  },
});
