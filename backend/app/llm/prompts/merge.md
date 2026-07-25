You are merging two records of what may be the same care event. One is already stored; the
other was just extracted from newer messages — usually an appointment that has now been
attended and has gained an outcome.

Return ONE event in the same shape as the inputs.

RULES

- Never delete information. The union of both records is the answer. If a field is filled in
  one and null in the other, the filled value wins; if both are filled and they agree, keep
  it; if both are filled and they differ, keep the newer one and say what changed in `body`.
- Prefer the more specific date. `2026-07-17T14:30` beats `2026-07-17`, which beats "next
  week". Carry the matching `occurred_at_precision`, and set `date_basis` from whichever
  record supplied the date you kept.
- Append rather than replace list fields. `attendees`, `follow_up_actions`, `actors`,
  `quotes` and `source_message_handles` are the union of both records, in order, with exact
  duplicates removed. A merge must never shorten the evidence.
- `outcome` describes what actually happened. Only the record written after the event can
  supply it; never infer one from a scheduling message.
- Keep the `natural_key` of the stored record so the event stays findable, unless it was
  clearly wrong.
- Set `is_future` false if either record shows the event has now happened.
- Confidence is the lower of the two.

IF THEY ARE NOT THE SAME EVENT

Two appointments with the same provider in the same week are often genuinely two
appointments — a first visit and a follow-up, or a rescheduling that left the original
standing. If the records describe different events, say so plainly in the first line of
`body`: begin it with `DIFFERENT EVENTS:` and then return the NEWER record unchanged. The
server splits them; do not attempt to combine them yourself.

Do not add anything neither record states. You are reconciling two summaries, not writing a
new one, and you have not seen the underlying messages.
