import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const api = process.env.FLEET_API ?? "http://127.0.0.1:8710";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": api, "/files": api },
  },
});
