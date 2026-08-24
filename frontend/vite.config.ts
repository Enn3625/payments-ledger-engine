import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // strictPort, because silently drifting to 5174 when 5173 is taken puts the
  // app on an origin the backend's CORS allowlist does not include, and the
  // resulting failure looks like a dead API. Fail loudly on the port instead.
  server: { port: 5173, strictPort: true },
  build: { outDir: "dist", sourcemap: true },
});
