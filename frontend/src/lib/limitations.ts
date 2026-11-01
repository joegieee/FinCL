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
