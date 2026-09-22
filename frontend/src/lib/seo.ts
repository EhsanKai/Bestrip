const HOME_TITLE = "Detoura | Extraordinary trips within reach";
const HOME_DESCRIPTION =
  "Detoura helps you discover non-obvious journeys shaped around your budget, dates, travel style, and time.";
const SOCIAL_IMAGE_PATH = "/brand/detoura-brand-preview.png";

function configuredSiteUrl(): string | null {
  const raw = import.meta.env.VITE_PUBLIC_SITE_URL as string | undefined;
  const trimmed = raw?.trim().replace(/\/+$/, "");
  if (!trimmed) return null;
  try {
    const url = new URL(trimmed);
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    return url.toString().replace(/\/+$/, "");
  } catch {
    return null;
  }
}

function absolute(path: string): string {
  const siteUrl = configuredSiteUrl();
  return siteUrl ? new URL(path, `${siteUrl}/`).toString() : path;
}

function upsertMeta(selector: string, create: () => HTMLMetaElement, content: string) {
  let element = document.head.querySelector<HTMLMetaElement>(selector);
  if (!element) {
    element = create();
    document.head.append(element);
  }
  element.content = content;
}

function remove(selector: string) {
  document.head.querySelectorAll(selector).forEach((element) => element.remove());
}

function setNameMeta(name: string, content: string) {
  upsertMeta(
    `meta[name="${name}"]`,
    () => {
      const element = document.createElement("meta");
      element.name = name;
      return element;
    },
    content,
  );
}

function setPropertyMeta(property: string, content: string) {
  upsertMeta(
    `meta[property="${property}"]`,
    () => {
      const element = document.createElement("meta");
      element.setAttribute("property", property);
      return element;
    },
    content,
  );
}

function setCanonical(href: string | null) {
  remove('link[rel="canonical"]');
  if (!href) return;
  const element = document.createElement("link");
  element.rel = "canonical";
  element.href = href;
  document.head.append(element);
}

function setJsonLd() {
  remove("#detoura-website-jsonld");
  const siteUrl = configuredSiteUrl();
  if (!siteUrl) return;
  const element = document.createElement("script");
  element.id = "detoura-website-jsonld";
  element.type = "application/ld+json";
  element.textContent = JSON.stringify({
    "@context": "https://schema.org",
    "@type": "WebSite",
    name: "Detoura",
    url: `${siteUrl}/`,
    description: HOME_DESCRIPTION,
  });
  document.head.append(element);
}

function clearPublicSocialMetadata() {
  remove('meta[property^="og:"]');
  remove('meta[name^="twitter:"]');
  remove("#detoura-website-jsonld");
}

export function applyPublicHomeSeo() {
  const canonical = absolute("/");
  const image = absolute(SOCIAL_IMAGE_PATH);
  document.title = HOME_TITLE;
  setNameMeta("description", HOME_DESCRIPTION);
  setNameMeta("robots", "index,follow");
  setCanonical(canonical);
  setPropertyMeta("og:title", HOME_TITLE);
  setPropertyMeta("og:description", HOME_DESCRIPTION);
  setPropertyMeta("og:type", "website");
  setPropertyMeta("og:url", canonical);
  setPropertyMeta("og:image", image);
  setPropertyMeta("og:site_name", "Detoura");
  setNameMeta("twitter:card", "summary_large_image");
  setNameMeta("twitter:title", HOME_TITLE);
  setNameMeta("twitter:description", HOME_DESCRIPTION);
  setNameMeta("twitter:image", image);
  setJsonLd();
}

export function applyNoIndexSeo(title: string, description: string) {
  document.title = title;
  setNameMeta("description", description);
  setNameMeta("robots", "noindex,nofollow");
  setCanonical(null);
  clearPublicSocialMetadata();
}

export function applyPrivacySeo(title: string, description: string, ready: boolean) {
  document.title = `${title} | Detoura`;
  setNameMeta("description", description);
  setNameMeta("robots", ready ? "index,follow" : "noindex,nofollow");
  setCanonical(absolute("/privacy"));
  clearPublicSocialMetadata();
}
