// Public legal content only. Supply verified values here; never credentials.
// Engineering completion does not approve this configuration for release.
export const legalConfig = {
  LEGAL_ENTITY_NAME: "",
  REGISTERED_ADDRESS: "",
  PRIVACY_CONTACT_EMAIL: "",
  PRIVACY_NOTICE_EFFECTIVE_DATE: "",
  COMPETENT_SUPERVISORY_AUTHORITY_NAME: "",
  COMPETENT_SUPERVISORY_AUTHORITY_URL: "",
  COMPANION_TRAVELLER_LEGAL_NOTICE_APPROVED: false,
};

export type LegalConfig = typeof legalConfig;
export const requiredLegalFields = [
  "LEGAL_ENTITY_NAME", "REGISTERED_ADDRESS", "PRIVACY_CONTACT_EMAIL",
  "PRIVACY_NOTICE_EFFECTIVE_DATE", "COMPETENT_SUPERVISORY_AUTHORITY_NAME",
  "COMPETENT_SUPERVISORY_AUTHORITY_URL",
] as const;

export function legalReadinessIssues(config: LegalConfig = legalConfig): string[] {
  const issues: string[] = [];
  for (const key of requiredLegalFields) {
    const value = config[key];
    if (typeof value !== "string" || !value.trim() ||
        /[[\]<>]|placeholder|\b(TODO|TBD|unconfigured|example|test)\b/i.test(value)) {
      issues.push(`${key}: missing or draft value`);
    }
  }
  if (!/^[^\s@?&#]+@[^\s@?&#]+\.[^\s@?&#]+$/.test(config.PRIVACY_CONTACT_EMAIL) ||
      /\.(invalid|localhost|test)$/i.test(config.PRIVACY_CONTACT_EMAIL)) {
    issues.push("PRIVACY_CONTACT_EMAIL: valid privacy contact required");
  }
  const date = config.PRIVACY_NOTICE_EFFECTIVE_DATE;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !Number.isFinite(Date.parse(date)) ||
      new Date(date).toISOString().slice(0, 10) !== date) {
    issues.push("PRIVACY_NOTICE_EFFECTIVE_DATE: valid ISO calendar date required");
  }
  try {
    const url = new URL(config.COMPETENT_SUPERVISORY_AUTHORITY_URL);
    if (url.protocol !== "https:" || url.username || url.password ||
        !url.hostname.includes(".") || /\.(invalid|test|localhost)$/.test(url.hostname)) throw Error();
  } catch { issues.push("COMPETENT_SUPERVISORY_AUTHORITY_URL: official HTTPS URL required"); }
  if (config.COMPANION_TRAVELLER_LEGAL_NOTICE_APPROVED !== true) {
    issues.push("COMPANION_TRAVELLER_LEGAL_NOTICE_APPROVED: unresolved legal gate");
  }
  return issues;
}
