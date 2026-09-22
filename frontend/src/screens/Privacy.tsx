import { useEffect, useState } from "react";
import { BrandLogo } from "../components/ui/BrandLogo";
import { applyPrivacySeo } from "../lib/seo";
import { legalConfig, legalReadinessIssues } from "../privacy/legalConfig";
import { noticeLabels, noticeSections, type PrivacyLanguage } from "../privacy/noticeContent";
import "./Privacy.css";

export function Privacy() {
  const [language, setLanguage] = useState<PrivacyLanguage>(() =>
    new URLSearchParams(window.location.search).get("lang") === "de" ? "de" : "en");
  const labels = noticeLabels[language];
  const issues = legalReadinessIssues();
  const ready = issues.length === 0;
  const showNotice = ready || import.meta.env.DEV;
  useEffect(() => {
    const previous = document.documentElement.lang;
    document.documentElement.lang = language;
    applyPrivacySeo(labels.title, labels.description, ready);
    return () => { document.documentElement.lang = previous; };
  }, [language, labels, ready]);
  function changeLanguage(next: PrivacyLanguage) {
    setLanguage(next);
    const url = new URL(window.location.href);
    url.searchParams.set("lang", next);
    window.history.replaceState(window.history.state, "", url);
  }
  return (
    <div className="privacy-page">
      <header className="privacy-page__header container">
        <a href="/" aria-label={labels.home}><BrandLogo /></a>
        <a href="/">{labels.home}</a>
      </header>
      <main className="privacy-notice" lang={language} aria-labelledby="privacy-title">
        <label className="privacy-notice__language">
          {labels.language}
          <select value={language} onChange={event => changeLanguage(event.target.value === "de" ? "de" : "en")}>
            <option value="en" lang="en">English</option>
            <option value="de" lang="de">Deutsch</option>
          </select>
        </label>
        <h1 id="privacy-title">{labels.title}</h1>
        <p>{labels.description}</p>
        {!ready && <p className="privacy-notice__draft" role="status">{import.meta.env.DEV ? labels.draft : labels.unavailable}</p>}
        {showNotice && <>
          <p>{labels.effective}: {legalConfig.PRIVACY_NOTICE_EFFECTIVE_DATE || labels.missing}</p>
          <nav aria-label={labels.contents}>
            <ol>{noticeSections.map((section, index) => <li key={section.id}>
              <a href={`#${section.id}`}>{index + 1}. {section[language].title}</a>
            </li>)}</ol>
          </nav>
          {noticeSections.map((section, index) => <section key={section.id} id={section.id} aria-labelledby={`${section.id}-title`}>
            <h2 id={`${section.id}-title`}>{index + 1}. {section[language].title}</h2>
            {section[language].paragraphs.map(text => <p key={text}>{text}</p>)}
            {section.id === "controller" && <address>
              <p>{legalConfig.LEGAL_ENTITY_NAME || labels.missing}</p>
              <p>{legalConfig.REGISTERED_ADDRESS || labels.missing}</p>
              {legalConfig.PRIVACY_CONTACT_EMAIL && !issues.some(issue => issue.startsWith("PRIVACY_CONTACT_EMAIL"))
                ? <a href={`mailto:${legalConfig.PRIVACY_CONTACT_EMAIL}`}>{legalConfig.PRIVACY_CONTACT_EMAIL}</a> : <p>{labels.missing}</p>}
            </address>}
            {section.id === "authority" && (legalConfig.COMPETENT_SUPERVISORY_AUTHORITY_NAME && !issues.some(issue => issue.startsWith("COMPETENT_SUPERVISORY_AUTHORITY"))
              ? <a href={legalConfig.COMPETENT_SUPERVISORY_AUTHORITY_URL} rel="noreferrer">{legalConfig.COMPETENT_SUPERVISORY_AUTHORITY_NAME}</a> : <p>{labels.missing}</p>)}
          </section>)}
        </>}
      </main>
    </div>
  );
}
