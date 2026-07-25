/**
 * "This household has never been set up" is a state we derive, not a flag we are given.
 *
 * When Penny is added to a WhatsApp group she provisions the household from the webhook, and at
 * that moment nobody has told her who the family is caring for. `households.care_recipient_name`
 * is NOT NULL, so the row is created holding `settings.onboarding_placeholder_care_recipient` —
 * and that placeholder still being there IS the signal that first-run setup has not happened.
 * There is no `onboarded_at` column to read and no extra endpoint to call.
 *
 * The string is duplicated here rather than served from `/api/me` because it is a fixed value in
 * the frozen onboarding interface, and a second network round trip to learn a constant would be
 * a worse trade. If it ever changes, this line and `backend/app/config.py` change together.
 */
export const PLACEHOLDER_CARE_RECIPIENT = 'your family member'

/**
 * Compared case- and whitespace-insensitively, because the placeholder can come back through a
 * PATCH the user never meant to make (opening Settings and pressing Save re-sends it verbatim),
 * and because a household still holding it is not "nearly set up" — it is not set up.
 *
 * An empty name counts too: it cannot be put in a heading, and it is exactly as useless to
 * extraction as the placeholder is.
 */
export function isPlaceholderCareRecipient(name: string | null | undefined): boolean {
  if (name === null || name === undefined) return false
  const trimmed = name.trim()
  return trimmed.length === 0 || trimmed.toLowerCase() === PLACEHOLDER_CARE_RECIPIENT
}

/** Signed out is not first run: the login screen has its own job. */
export function needsFirstRunSetup(
  session: { household: { care_recipient_name: string } } | null | undefined,
): boolean {
  return session ? isPlaceholderCareRecipient(session.household.care_recipient_name) : false
}
