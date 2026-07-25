/**
 * Importing a WhatsApp export, in two screens: upload, then CONFIRM.
 *
 * The second screen is the entire mitigation for the one bug in this product that nothing throws
 * on. `17/03/2026` and `03/17/2026` are both valid dates, WhatsApp exports carry no timezone
 * offset at all, and an export spanning fewer than twelve days contains no proof either way. Read
 * a day-first file month-first and every date moves by months: the feed reorders, "two weeks ago"
 * means the wrong fortnight, and the family only finds out when a date contradicts something they
 * remember. There is no error to catch and no way back except deleting everything and re-importing.
 *
 * So the server states its reading, and this screen shows the evidence for it — the first and last
 * messages exactly as they appear in the file — and makes disagreeing with it one click. It is
 * built to be read rather than clicked through: the confirm button says how many messages and how
 * much it will cost, the parse losses are visible rather than buried, and when the file gives no
 * clean signal nothing is pre-selected at all, so the wizard cannot be finished without a human
 * having actually chosen.
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { useQueryClient } from '@tanstack/react-query'

import { queryKeys, useImportStatus, usePreviewImport, useStartImport } from '../api/queries'
import { ErrorState } from '../components'
import { formatEvidenceStamp } from '../lib/datetime'
import type { ImportPreview, ImportState, PreviewMessage } from '../types/api'
import { TimezoneField } from './timezone-field'
import styles from './routes.module.css'

/** A clean signal may be pre-selected; anything else has to be an explicit human choice. */
function evidenceIsClean(preview: ImportPreview): boolean {
  const evidence = preview.sniffed.dayfirst_evidence
  return evidence === 'day>12' || evidence === 'month>12'
}

/**
 * Why the server is sure — or why it isn't — in the family's words rather than the parser's.
 *
 * A lookup with a fallback rather than an exhaustive switch: the backend types this field as a
 * plain string, so a value added later must degrade to "check it yourself" rather than render a
 * blank space where the reason should be.
 */
const EVIDENCE_SENTENCE: Record<string, string | undefined> = {
  'day>12':
    'This file contains a date whose first number is above 12, so it can only be day-first.',
  'month>12':
    'This file contains a date whose second number is above 12, so it can only be month-first.',
  conflict:
    'This file contains dates that contradict each other — some read only one way, some only the other. Check the two messages above carefully.',
  none: 'Every date in this file reads correctly both ways, so nothing in it can settle the question. Please check the two messages above against what you remember.',
}

function evidenceSentence(preview: ImportPreview): string {
  return (
    EVIDENCE_SENTENCE[preview.sniffed.dayfirst_evidence] ??
    'Penny could not prove which way round these dates are. Please check the two messages above against what you remember.'
  )
}

export function ImportRoute() {
  const [file, setFile] = useState<File | null>(null)
  const [importId, setImportId] = useState<string | null>(null)

  const preview = usePreviewImport()
  const parsed = preview.data

  if (importId !== null) return <ProgressScreen importId={importId} />

  if (parsed && file) {
    return (
      <ConfirmScreen
        preview={parsed}
        file={file}
        onStarted={setImportId}
        onBack={() => {
          preview.reset()
          setFile(null)
        }}
      />
    )
  }

  return (
    <UploadScreen
      file={file}
      onPick={(picked) => {
        setFile(picked)
        preview.reset()
      }}
      onCheck={() => {
        if (file) preview.mutate(file)
      }}
      isChecking={preview.isPending}
      error={preview.error}
    />
  )
}

/* ------------------------------------------------------------------------ screen 1: upload ---- */

function UploadScreen({
  file,
  onPick,
  onCheck,
  isChecking,
  error,
}: {
  file: File | null
  onPick: (file: File | null) => void
  onCheck: () => void
  isChecking: boolean
  error: unknown
}) {
  return (
    <>
      <div className={styles.pageHeader}>
        <h1>Import a WhatsApp export</h1>
        <p className={styles.hint}>Step 1 of 2 — choose the file.</p>
      </div>

      <section className={styles.section}>
        <ol className={styles.steps}>
          <li>Open the family group in WhatsApp.</li>
          <li>
            Tap the group name, scroll to the bottom, and choose <strong>Export chat</strong>.
          </li>
          <li>
            Choose <strong>Without media</strong>. Penny only reads text, and the photos would make
            the file enormous.
          </li>
          <li>
            Save the <code>.txt</code> somewhere you can find it, then pick it below.
          </li>
        </ol>
        <p className={styles.hint}>
          Nothing is saved when you pick the file. Penny reads it in memory, shows you what it
          found, and waits for you to confirm before storing a single message.
        </p>
      </section>

      {error ? <ErrorState error={error} title="That file could not be read" /> : null}

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
              onChange={(change) => onPick(change.target.files?.[0] ?? null)}
            />
            {file ? (
              <span className={styles.hint}>
                {file.name} · {Math.max(1, Math.round(file.size / 1024)).toLocaleString('en-GB')} KB
              </span>
            ) : null}
          </div>
          <div className={styles.actions}>
            <button
              type="button"
              className={`${styles.button} ${styles.buttonPrimary}`}
              disabled={!file || isChecking}
              onClick={onCheck}
            >
              {isChecking ? 'Reading the file…' : 'Check the file'}
            </button>
          </div>
        </div>
      </section>
    </>
  )
}

/* ----------------------------------------------------------------------- screen 2: confirm ---- */

function ConfirmScreen({
  preview,
  file,
  onStarted,
  onBack,
}: {
  preview: ImportPreview
  file: File
  onStarted: (importId: string) => void
  onBack: () => void
}) {
  const clean = evidenceIsClean(preview)
  const [dayfirst, setDayfirst] = useState<boolean | null>(clean ? preview.sniffed.dayfirst : null)
  const [timezone, setTimezone] = useState(preview.sniffed.timezone)
  // Already open when the file cannot settle it: there is nothing to agree with yet.
  const [disputing, setDisputing] = useState(!clean)
  const start = useStartImport()

  const head = preview.report.preview_head[0]
  const tail = preview.report.preview_tail.at(-1)
  // Rendered in the zone the server parsed with, which reproduces the clock time written in the
  // file exactly. Re-rendering the evidence in a zone the user is still choosing would show times
  // that appear neither in the file nor in the database.
  const evidenceZone = preview.sniffed.timezone

  return (
    <>
      <div className={styles.pageHeader}>
        <h1>Does this look right?</h1>
        <p className={styles.hint}>Step 2 of 2 — check the dates. Nothing has been saved yet.</p>
      </div>

      {start.error ? <ErrorState error={start.error} title="That import did not start" /> : null}

      <section className={styles.section}>
        <div className={styles.evidence}>
          <p className={styles.evidenceLine}>
            <span className={styles.evidenceLabel}>First message</span>
            <MessageLine message={head} timeZone={evidenceZone} />
          </p>
          <p className={styles.evidenceLine}>
            <span className={styles.evidenceLabel}>Last message</span>
            <MessageLine message={tail} timeZone={evidenceZone} />
          </p>
          <p className={styles.evidenceNote}>
            Read as <strong>{preview.sniffed.dayfirst ? 'day first' : 'month first'}</strong> (
            {preview.sniffed.dayfirst ? '17/03/2026 is 17 March' : '03/17/2026 is 17 March'}), in{' '}
            <strong>{preview.sniffed.timezone}</strong>. {evidenceSentence(preview)}
          </p>
        </div>

        {disputing ? (
          <div className={styles.form} style={{ marginTop: 16, maxWidth: 560 }}>
            <fieldset className={styles.fieldset}>
              <legend className={styles.label}>How the dates are written</legend>
              <label className={styles.choice}>
                <input
                  type="radio"
                  name="dayfirst"
                  checked={dayfirst === true}
                  onChange={() => setDayfirst(true)}
                />
                <span>
                  <strong>Day first</strong> — 17/03/2026 means 17 March. Usual in the UK, Europe
                  and Australia.
                </span>
              </label>
              <label className={styles.choice}>
                <input
                  type="radio"
                  name="dayfirst"
                  checked={dayfirst === false}
                  onChange={() => setDayfirst(false)}
                />
                <span>
                  <strong>Month first</strong> — 03/17/2026 means 17 March. Usual in the US.
                </span>
              </label>
              {dayfirst !== null && dayfirst !== preview.sniffed.dayfirst ? (
                <p className={styles.warn}>
                  The two messages above were read the other way round, so every date will move once
                  the import runs. Only choose this if the dates above look wrong to you.
                </p>
              ) : null}
            </fieldset>

            <TimezoneField
              id="import-timezone"
              label="Where the family was writing from"
              value={timezone}
              onChange={setTimezone}
              hint="Exports carry no timezone at all, so this is what turns 21:04 in the file into a real moment."
            />
          </div>
        ) : (
          <div className={styles.actions} style={{ marginTop: 14 }}>
            <button type="button" className={styles.button} onClick={() => setDisputing(true)}>
              That's not right
            </button>
            <span className={styles.hint}>
              If either date above is off, every date in the history will be.
            </span>
          </div>
        )}
      </section>

      <section className={styles.section}>
        <h2>What Penny found</h2>
        <dl className={styles.facts}>
          <dt className={styles.factLabel}>File</dt>
          <dd className={styles.factValue}>{preview.filename}</dd>
          <dt className={styles.factLabel}>Messages</dt>
          <dd className={styles.factValue}>{preview.report.messages.toLocaleString('en-GB')}</dd>
          <dt className={styles.factLabel}>People</dt>
          <dd className={styles.factValue}>
            {Object.entries(preview.report.senders)
              .sort(([, a], [, b]) => b - a)
              .map(([sender, count]) => `${sender} (${count.toLocaleString('en-GB')})`)
              .join(', ')}
          </dd>
          <dt className={styles.factLabel}>Skipped</dt>
          <dd className={styles.factValue}>
            {preview.report.media_placeholders.toLocaleString('en-GB')} photos and attachments,{' '}
            {preview.report.system_lines.toLocaleString('en-GB')} system lines,{' '}
            {preview.report.unparsed_lines.toLocaleString('en-GB')} lines Penny could not read
          </dd>
          <dt className={styles.factLabel}>Cost to read</dt>
          <dd className={styles.factValue}>
            about ${preview.estimate.estimated_cost_usd}, taking around{' '}
            {preview.estimate.estimated_minutes} minute
            {preview.estimate.estimated_minutes === 1 ? '' : 's'}
          </dd>
        </dl>

        {preview.estimate.over_budget ? (
          <p className={styles.warn}>
            That is over the ${preview.estimate.budget_usd} limit for one import, so it will be
            refused. Export a shorter date range and try again.
          </p>
        ) : null}

        {preview.report.unparsed_samples.length > 0 ? (
          <details className={styles.details}>
            <summary>Show the lines Penny could not read</summary>
            <ul>
              {preview.report.unparsed_samples.map((sample) => (
                <li key={sample}>
                  <code>{sample}</code>
                </li>
              ))}
            </ul>
            <p className={styles.hint}>
              These are almost always attachment placeholders and system notices, and skipping them
              is expected.
            </p>
          </details>
        ) : null}
      </section>

      <div className={styles.actions}>
        <button
          type="button"
          className={`${styles.button} ${styles.buttonPrimary}`}
          disabled={dayfirst === null || start.isPending || preview.estimate.over_budget}
          onClick={() => {
            if (dayfirst === null) return
            start.mutate(
              { file, dayfirst, timezone },
              { onSuccess: (accepted) => onStarted(accepted.import_id) },
            )
          }}
        >
          {start.isPending
            ? 'Starting…'
            : `Yes — import ${preview.report.messages.toLocaleString('en-GB')} messages`}
        </button>
        <button type="button" className={styles.button} onClick={onBack} disabled={start.isPending}>
          Choose a different file
        </button>
        {dayfirst === null ? (
          <span className={styles.hint}>Choose how the dates are written first.</span>
        ) : null}
      </div>
    </>
  )
}

/** "Tue 14 Jul 2026, 21:04 — Sarah: Morning all". The point is that it is quotable back at you. */
function MessageLine({
  message,
  timeZone,
}: {
  message: PreviewMessage | undefined
  timeZone: string
}) {
  if (!message) return <span className={styles.hint}>none found</span>
  return (
    <span>
      <span className={styles.stamp}>{formatEvidenceStamp(message.sent_at, timeZone)}</span> —{' '}
      {/* A system line has no sender and a media placeholder has no text. Show the gap rather
          than inventing a name: a made-up attribution beside a real timestamp is worse. */}
      <strong>{message.sender ?? 'unknown sender'}</strong>: {message.text ?? '(no text)'}
    </span>
  )
}

/* ---------------------------------------------------------------------- screen 3: progress ---- */

const PHASE: Record<ImportState, string> = {
  pending: 'Queued',
  importing: 'Saving the messages',
  extracting: 'Reading the conversation',
  complete: 'All done',
  failed: 'Import stopped',
}

function ProgressScreen({ importId }: { importId: string }) {
  const status = useImportStatus(importId)
  const client = useQueryClient()
  const data = status.data
  const done = data?.status === 'complete'

  // The feed, the panel and the message count are all stale the moment extraction finishes.
  useEffect(() => {
    if (!done) return
    void client.invalidateQueries({ queryKey: queryKeys.feed })
    void client.invalidateQueries({ queryKey: queryKeys.upcoming })
    void client.invalidateQueries({ queryKey: queryKeys.session })
    void client.invalidateQueries({ queryKey: queryKeys.members })
  }, [done, client])

  const percent =
    data && data.inserted_count > 0
      ? Math.min(100, Math.round((data.extracted_count / data.inserted_count) * 100))
      : 0

  return (
    <>
      <div className={styles.pageHeader}>
        <h1>{data ? PHASE[data.status] : 'Starting…'}</h1>
        <p className={styles.hint}>
          This keeps going if you close the page — Penny picks up where it left off.
        </p>
      </div>

      {status.error ? <ErrorState error={status.error} title="Lost track of the import" /> : null}

      {data ? (
        <section className={styles.section}>
          <div className={styles.progress}>
            <div className={styles.progressBar} style={{ width: `${percent}%` }} />
          </div>
          <p className={styles.hint} style={{ marginTop: 8 }}>
            {data.inserted_count.toLocaleString('en-GB')} of{' '}
            {data.message_count.toLocaleString('en-GB')} messages saved
            {data.inserted_count > 0
              ? `, ${data.extracted_count.toLocaleString('en-GB')} read (${percent}%)`
              : ''}
            .
          </p>

          {data.status === 'failed' ? (
            <p className={styles.error} style={{ marginTop: 16 }}>
              {data.error ?? 'The import stopped before it finished.'}
            </p>
          ) : null}

          {done ? (
            <div className={styles.actions} style={{ marginTop: 20 }}>
              <Link className={`${styles.button} ${styles.buttonPrimary}`} to="/">
                See the history
              </Link>
            </div>
          ) : (
            <p className={styles.hint} style={{ marginTop: 16 }}>
              Penny is reading the conversation in batches and writing down the appointments,
              symptoms and medication changes it finds. Entries appear in the history as they land.
            </p>
          )}
        </section>
      ) : null}
    </>
  )
}
