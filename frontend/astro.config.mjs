// @ts-check
import { defineConfig } from "astro/config";
import sitemap from "@astrojs/sitemap";

export default defineConfig({
  site: "https://imagory.mshahnawaz.me",
  output: "static",
  trailingSlash: "never",
  build: { format: "file" },
  integrations: [sitemap()],
});
