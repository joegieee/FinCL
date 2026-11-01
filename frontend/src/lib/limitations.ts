/**
 * Limitations the backend reports for audit, not for the reader.
 *
 * The forecast manifest records that `position_page` is read unpinned beside the
 * pinned inputs (forecast/manifest.py `_POSITION_NOT_PINNED`). That is true and
 * belongs in the manifest and on the Forecast tab's audit trail, but on a
 * summary card it reads as an error the person is expected to act on, and
 * there is nothing they can do about it.
 */
const AUDIT_ONLY = [/^position_page is not generation-pinned/]

export function readerLimitations(limitations: string[]): string[] {
  return limitations.filter(text => !AUDIT_ONLY.some(pattern => pattern.test(text))).map(text => text.replace(/^debt:[0-9a-f-]{36}/, 'A debt'))
}

const REASONS: Record<string, string> = {
  NO_EVIDENCE_COVERAGE: "Your statements don't cover this whole period.",
  NO_SUPPORTED_FORECAST_EVIDENCE: "There isn't enough statement history to forecast this.",
  NO_COMBINED_MINIMUM_EVIDENCE: "Not every account can be forecast, so there is no combined lowest point.",
  NO_NET_WORTH_EVIDENCE: "No dated balances support this figure yet.",
  NO_DATED_BREAKDOWN: "This figure has no breakdown by date.",
  RATE_NOT_IMPORTED: "No exchange rate has been imported for this date.",
  RATE_PATH_NOT_CONFIGURED: "No exchange rate is set up for this currency.",
  PERIOD_UNKNOWN: "The date of this amount isn't known.",
}

/** The sentence for a reason code from the API; text that is already a sentence passes through. */
export function reasonText(reason: string): string {
  return REASONS[reason] ?? reason
}

/** Reads an enum value from the API as words: `NOT_RECOGNISED` becomes "Not recognised". */
export function humanize(value: string): string {
  const words = value.replaceAll('_', ' ').toLowerCase()
  return words.charAt(0).toUpperCase() + words.slice(1)
}
