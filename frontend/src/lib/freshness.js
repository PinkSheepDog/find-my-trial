// Corpus freshness. /health reports `data_current_through` (the latest registry
// "Last Update Posted" in the indexed corpus) and `normalization_version`.
//
// This drives real referral decisions: a trial that closed to accrual last month
// still looks open in a stale corpus, so the age of the data is shown next to
// every result rather than being fetched and dropped. When the date is missing
// or unparseable the UI says so — it never implies freshness it cannot prove.

export const STALE_AFTER_DAYS = 45;

function parseDate(raw) {
  if (typeof raw !== "string" || !raw.trim()) return null;
  const d = new Date(raw.trim());
  return Number.isNaN(d.getTime()) ? null : d;
}

/**
 * @returns {{known: boolean, raw: string, label: string, ageDays: number|null,
 *            stale: boolean, note: string, normalizationVersion: string}}
 */
export function corpusFreshness(health, now = new Date()) {
  const raw = (health && health.data_current_through) || "";
  const normalizationVersion = (health && health.normalization_version) || "";
  const parsed = parseDate(raw);

  if (!raw) {
    return {
      known: false,
      raw: "",
      label: "unknown",
      ageDays: null,
      stale: true,
      note: "Corpus date unavailable — confirm trial status on ClinicalTrials.gov before referral.",
      normalizationVersion,
    };
  }

  if (!parsed) {
    // Unparseable but present: show it verbatim rather than hiding it.
    return {
      known: true, raw, label: raw, ageDays: null, stale: false,
      note: "", normalizationVersion,
    };
  }

  const ageDays = Math.max(0, Math.floor((now.getTime() - parsed.getTime()) / 86_400_000));
  const label = parsed.toISOString().slice(0, 10);
  const stale = ageDays > STALE_AFTER_DAYS;
  return {
    known: true,
    raw,
    label,
    ageDays,
    stale,
    note: stale
      ? `Corpus is ${ageDays} days old — recruitment status may have changed. Re-verify before referral.`
      : "",
    normalizationVersion,
  };
}

/**
 * Corpus identity and integrity, read defensively — every field is optional and
 * older servers send none of them.
 *
 * `corpus_integrity_verified: false` means the corpus was accepted WITHOUT a
 * digest check. That is reported as "unverified", never smoothed into silence:
 * claiming provenance that was not checked is worse than admitting it was not.
 */
export function corpusProvenance(health) {
  const h = health || {};
  const verified = h.corpus_integrity_verified;
  return {
    appVersion: h.app_version || "",
    contentHash: h.corpus_content_hash || "",
    indexBuiltAt: h.index_built_at || "",
    integrityKnown: typeof verified === "boolean",
    integrityVerified: verified === true,
    integrityLabel:
      typeof verified !== "boolean"
        ? "not reported"
        : verified
          ? "verified"
          : "UNVERIFIED — corpus accepted without a digest check",
    deidReviewEnforced: h.deid_review_enforced,
  };
}

/** "today" / "3 days old" / "" when the age is unknown. */
export function ageLabel(ageDays) {
  if (ageDays == null) return "";
  if (ageDays === 0) return "updated today";
  if (ageDays === 1) return "1 day old";
  return `${ageDays} days old`;
}
