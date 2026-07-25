import { useState } from 'react'

import { isImportFinished, usePreviewImport, useImportStatus, useStartImport } from '../api/queries'
import { formatDate, formatTime } from '../lib/datetime'
import type { ImportPreview } from '../types/api'
import styles from './routes.module.css'

/** A clean signal means we can pre-select; anything else must be an explicit human choice. */
function evidenceIsClean(preview: ImportPreview): boolean {
  return preview.sniffed.dayfirst_evidence === 'day>12' || preview.sniffed.dayfirst_evidence === 'month>12'
}

/**
 * Two screens on purpose: preview, then confirm.
 *
 * `dd/mm` vs `mm/dd` is undecidable from an export spanning under twelve days, and exports carry
 * no timezone offset at all. A wrong guess shifts every date by months, misorders the feed and
 * anchors every "yesterday" during extraction to the wrong day — with nothing throwing. So the
 * server suggests and the user confirms.
 */
export function ImportRoute() {
  const [file, setFile] = useState<File | null>(null)
  const [dayfirst, setDayfirst] = useState<boolean | null>(null)
  const [timezone, setTimezone] = useState('Europe/London')
  const [importId, setImportId] = useState<string | null>(null)

  const preview = usePreviewImport()
  const start = useStartImport()
  const status = useImportStatus(importId)

  const parsed = preview.data
  const error = preview.error ?? start.error ?? status.error

  return (
    <>
      <div className={styles.pageHeader}>
        <h1>Import a WhatsApp export</h1>
        <p className={styles.hint}>
          Export the family group from WhatsApp without media, then upload the <code>.txt</code>.
        </p>
      </div>

      {error ? <p className={styles.error}>{error.message}</p> : null}

      {importId === null ? (
        <section className={styles.section}>
          <div className={styles.form}>
            <div className={styles.field}>
              <label className={styles.label} htmlFor="export-file">
                Chat export
              </label>
              <input
                id="export-file"
                className={styles.input}
                type="file"
                accept=".txt,text/plain"
                onChange={(change) => {
                  setFile(change.target.files?.[0] ?? null)
                  preview.reset()
                }}
              />
            </div>
            <div className={styles.actions}>
              <button
                type="button"
                className={styles.button}
                disabled={!file || preview.isPending}
                onClick={() => {
                  if (!file) return
                  preview.mutate(file, {
                    onSuccess: (result) => {
                      setDayfirst(evidenceIsClean(result) ? result.sniffed.dayfirst : null)
                      setTimezone(result.sniffed.timezone)
                    },
                  })
                }}
              >
                {preview.isPending ? 'Reading…' : 'Check the file'}
              </button>
            </div>
          </div>
        </section>
      ) : null}

      {parsed && importId === null ? (
        <section className={styles.section}>
          <h2>{parsed.filename}</h2>
          <dl className={styles.facts}>
            <dt className={styles.factLabel}>Messages</dt>
            <dd className={styles.factValue}>{parsed.report.messages}</dd>
            <dt className={styles.factLabel}>Senders</dt>
            <dd className={styles.factValue}>
              {Object.entries(parsed.report.senders)
                .map(([sender, count]) => `${sender} (${count})`)
                .join(', ')}
            </dd>
            <dt className={styles.factLabel}>Could not parse</dt>
            <dd className={styles.factValue}>{parsed.report.unparsed_lines} lines</dd>
            <dt className={styles.factLabel}>Estimated cost</dt>
            <dd className={styles.factValue}>
              ${parsed.estimate.estimated_cost_usd}, about {parsed.estimate.estimated_minutes}{' '}
              minutes
            </dd>
          </dl>

          <h3 style={{ marginTop: 20 }}>Does this look right?</h3>
          <p className={styles.hint}>
            The first and last messages, rendered with the timezone below applied.
          </p>
          {[...parsed.report.preview_head, ...parsed.report.preview_tail].map((message) => (
            <p key={`${message.sent_at}-${message.sender}`} className={styles.meta}>
              {formatDate(message.sent_at, timezone)} {formatTime(message.sent_at, timezone)} —{' '}
              {message.sender}: {message.text}
            </p>
          ))}

          <div className={styles.form} style={{ marginTop: 20 }}>
            <fieldset className={styles.field} style={{ border: 0, padding: 0, margin: 0 }}>
              <legend className={styles.label}>Date order</legend>
              {!evidenceIsClean(parsed) ? (
                <span className={styles.hint}>
                  This export gives no clean signal ({parsed.sniffed.dayfirst_evidence}). Pick the
                  one that matches the dates above.
                </span>
              ) : null}
              <label>
                <input
                  type="radio"
                  name="dayfirst"
                  checked={dayfirst === true}
                  onChange={() => setDayfirst(true)}
                />{' '}
                Day first (17/03/2026)
              </label>
              <label>
                <input
                  type="radio"
                  name="dayfirst"
                  checked={dayfirst === false}
                  onChange={() => setDayfirst(false)}
                />{' '}
                Month first (03/17/2026)
              </label>
            </fieldset>

            <div className={styles.field}>
              <label className={styles.label} htmlFor="import-timezone">
                Timezone the chat was written in
              </label>
              <input
                id="import-timezone"
                className={styles.input}
                value={timezone}
                onChange={(change) => setTimezone(change.target.value)}
              />
            </div>

            <div className={styles.actions}>
              <button
                type="button"
                className={`${styles.button} ${styles.buttonPrimary}`}
                disabled={!file || dayfirst === null || start.isPending}
                onClick={() => {
                  if (!file || dayfirst === null) return
                  start.mutate(
                    { file, dayfirst, timezone },
                    { onSuccess: (accepted) => setImportId(accepted.import_id) },
                  )
                }}
              >
                {start.isPending ? 'Starting…' : `Import ${parsed.report.messages} messages`}
              </button>
            </div>
          </div>
        </section>
      ) : null}

      {importId !== null && status.data ? (
        <section className={styles.section}>
          <h2>{status.data.status}</h2>
          <div className={styles.progress}>
            <div
              className={styles.progressBar}
              style={{
                width: `${
                  status.data.inserted_count === 0
                    ? 0
                    : Math.round((status.data.extracted_count / status.data.inserted_count) * 100)
                }%`,
              }}
            />
          </div>
          <p className={styles.hint}>
            {status.data.extracted_count} of {status.data.inserted_count} messages read.
          </p>
          {status.data.error ? <p className={styles.error}>{status.data.error}</p> : null}
          {isImportFinished(status.data) ? (
            <p className={styles.hint}>Done — the feed has been updated.</p>
          ) : null}
        </section>
      ) : null}
    </>
  )
}
