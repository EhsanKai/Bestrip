import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const HOME_TITLE = "Detoura | Extraordinary trips within reach";
const HOME_DESCRIPTION =
  "Detoura helps you discover non-obvious journeys shaped around your budget, dates, travel style, and time.";
const SOCIAL_IMAGE_PATH = "/brand/detoura-brand-preview.png";
const PUBLIC_ROUTES = ["/"] as const;
const PRIVATE_OR_TRANSIENT_PATHS = [
  "/ops",
  "/login",
  "/signup",
  "/account",
  "/my-trips",
  "/checkout",
  "/payment",
  "/booking",
  "/documents",
  "/search",
] as const;

function normalizeSiteUrl(value: string | undefined): string | null {
  const trimmed = value?.trim().replace(/\/+$/, "");
  if (!trimmed) return null;
  try {
    const url = new URL(trimmed);
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    return url.toString().replace(/\/+$/, "");
  } catch {
    return null;
  }
}

function absoluteUrl(siteUrl: string | null, path: string): string {
  return siteUrl ? new URL(path, `${siteUrl}/`).toString() : path;
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function seoPlugin(): Plugin {
  const siteUrl = normalizeSiteUrl(
    process.env.VITE_PUBLIC_SITE_URL ?? process.env.SITE_URL,
  );
  const canonical = absoluteUrl(siteUrl, "/");
  const socialImage = absoluteUrl(siteUrl, SOCIAL_IMAGE_PATH);
  // V9 staging/production ops readiness (docs/V9_TECHNICAL_SEO_FOUNDATION_
  // REPORT.md flagged this as unsolved): opt-in only, so a build that sets
  // nothing keeps today's production-indexable output unchanged. A staging
  // pipeline sets VITE_STAGING=true to get a build that is never indexable,
  // regardless of whether VITE_PUBLIC_SITE_URL also happens to be set.
  const isStaging = (process.env.VITE_STAGING ?? "").trim().toLowerCase() === "true";

  return {
    name: "detoura-seo-foundation",
    transformIndexHtml(html: string) {
      const jsonLd = siteUrl && !isStaging
        ? `<script type="application/ld+json">${JSON.stringify({
            "@context": "https://schema.org",
            "@type": "WebSite",
            name: "Detoura",
            url: `${siteUrl}/`,
            description: HOME_DESCRIPTION,
          })}</script>`
        : "";
      const tags = [
        `<title>${escapeHtml(HOME_TITLE)}</title>`,
        `<meta name="description" content="${escapeHtml(HOME_DESCRIPTION)}" />`,
        `<meta name="robots" content="${isStaging ? "noindex,nofollow" : "index,follow"}" />`,
        `<link rel="canonical" href="${escapeHtml(canonical)}" />`,
        `<meta property="og:title" content="${escapeHtml(HOME_TITLE)}" />`,
        `<meta property="og:description" content="${escapeHtml(HOME_DESCRIPTION)}" />`,
        '<meta property="og:type" content="website" />',
        `<meta property="og:url" content="${escapeHtml(canonical)}" />`,
        `<meta property="og:image" content="${escapeHtml(socialImage)}" />`,
        '<meta property="og:site_name" content="Detoura" />',
        '<meta name="twitter:card" content="summary_large_image" />',
        `<meta name="twitter:title" content="${escapeHtml(HOME_TITLE)}" />`,
        `<meta name="twitter:description" content="${escapeHtml(HOME_DESCRIPTION)}" />`,
        `<meta name="twitter:image" content="${escapeHtml(socialImage)}" />`,
        jsonLd,
      ].filter(Boolean).join("\n    ");
      return html.replace("<!-- detoura:seo -->", tags);
    },
    generateBundle() {
      const robots = isStaging
        ? [
            "User-agent: *",
            "Disallow: /",
            "",
            "# Staging build (VITE_STAGING=true) - never indexable. See",
            "# docs/V9_STAGING_PRODUCTION_OPS_READINESS_REPORT.md.",
            "",
          ].join("\n")
        : [
            "User-agent: *",
            "Allow: /",
            ...PRIVATE_OR_TRANSIENT_PATHS.flatMap((path) => [
              `Disallow: ${path}`,
              `Disallow: ${path}/`,
            ]),
            "",
            "# robots.txt is crawl guidance only. Authentication and authorization remain the security boundary.",
            ...(siteUrl ? ["", `Sitemap: ${siteUrl}/sitemap.xml`] : []),
            "",
          ].join("\n");

      const sitemap = siteUrl && !isStaging
        ? [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
            ...PUBLIC_ROUTES.map((path) =>
              [
                "  <url>",
                `    <loc>${absoluteUrl(siteUrl, path)}</loc>`,
                "    <changefreq>weekly</changefreq>",
                "    <priority>1.0</priority>",
                "  </url>",
              ].join("\n"),
            ),
            "</urlset>",
            "",
          ].join("\n")
        : [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
            "  <!-- Set VITE_PUBLIC_SITE_URL at build time to emit absolute canonical URLs. -->",
            "</urlset>",
            "",
          ].join("\n");

      this.emitFile({ type: "asset", fileName: "robots.txt", source: robots });
      this.emitFile({ type: "asset", fileName: "sitemap.xml", source: sitemap });
    },
  };
}

export default defineConfig({
  plugins: [react(), seoPlugin()],
  server: {
    port: 5173,
    // The frontend talks to the same paths in dev and in production; only the
    // proxy differs, so no screen ever knows where the API lives.
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
