/**
 * Mock data for the care feed.
 *
 * Shape mirrors what Penny would produce from the WhatsApp engine: an entry is
 * Penny's reading of one or more real messages, and it always keeps the
 * messages it was drawn from so the reader can check the work.
 */

export type PersonId = "yuval" | "liz" | "owen" | "matthew" | "penny";

export type Person = {
  id: PersonId;
  name: string;
  initials: string;
  /** Tailwind class fragment for this person's colour. */
  tone: string;
  wash: string;
};

export const people: Record<PersonId, Person> = {
  yuval: { id: "yuval", name: "Yuval", initials: "Y", tone: "text-symptom", wash: "bg-symptom-wash" },
  liz: { id: "liz", name: "Liz", initials: "L", tone: "text-appointment", wash: "bg-appointment-wash" },
  owen: { id: "owen", name: "Owen", initials: "O", tone: "text-owen", wash: "bg-owen-wash" },
  matthew: { id: "matthew", name: "Matthew", initials: "M", tone: "text-wellbeing", wash: "bg-wellbeing-wash" },
  penny: { id: "penny", name: "Penny", initials: "P", tone: "text-penny", wash: "bg-penny-wash" },
};

export const personBg: Record<PersonId, string> = {
  yuval: "bg-symptom",
  liz: "bg-appointment",
  owen: "bg-owen",
  matthew: "bg-wellbeing",
  penny: "bg-penny",
};

export type Kind = "symptom" | "medication" | "appointment" | "wellbeing" | "admin";

export const kindStyle: Record<Kind, { label: string; text: string; bg: string }> = {
  symptom: { label: "Symptom", text: "text-symptom", bg: "bg-symptom-wash" },
  medication: { label: "Medication", text: "text-penny", bg: "bg-penny-wash" },
  appointment: { label: "Appointment", text: "text-appointment", bg: "bg-appointment-wash" },
  wellbeing: { label: "Wellbeing", text: "text-wellbeing", bg: "bg-wellbeing-wash" },
  admin: { label: "Admin", text: "text-admin", bg: "bg-admin-wash" },
};

/**
 * Who the record is about.
 *
 * Sits in the page header rather than the context column: the identity is the
 * frame for everything below it, not another panel competing with medications.
 */
export const person = {
  name: "Juno Alden",
  initials: "JA",
  age: 84,
  place: "Croydon",
  contributorCount: 4,
};

/**
 * The four facts a clinician asks for before anything else. Kept as a band
 * under the name because they are read together, at a glance, and never
 * scrolled to.
 */
export const vitals = [
  { label: "NHS number", value: "485 777 3456", alert: false },
  { label: "Allergies", value: "Penicillin · adhesive plasters", alert: true },
  { label: "RESPECT form", value: "On file · updated Jan 2026", link: "View", alert: false },
  {
    label: "Her baseline",
    value: "Mobile with frame · breathless on one flight of stairs",
    alert: false,
  },
];

export type Attachment = {
  kind: "photo" | "document";
  label: string;
  note: string;
  action: string;
};

export type Message = {
  from: PersonId;
  /** Penny's own messages sit on the right, like a sent bubble. */
  outbound?: boolean;
  text?: string;
  at: string;
  attachment?: Attachment;
  voice?: { duration: string; transcript: string };
};

export type Entry = {
  id: string;
  kind: Kind;
  /** Extra qualifier shown next to the kind, e.g. "New", "Changed". */
  qualifier?: string;
  headline: string;
  summary: string;
  /**
   * Everyone whose messages fed this entry. An entry is synthesised from a
   * span of conversation, not a single post, so it has no one author — the
   * entry shows the group rather than picking a misleading first name.
   */
  people: PersonId[];
  where: string;
  at: string;
  /** Date label in the timeline's left gutter, e.g. "Sat 25 Jul". */
  date: string;
  /**
   * The year the entry falls in. Kept off the date label on purpose: the
   * timeline prints the year once, where it changes, so twenty entries in the
   * same year do not repeat it twenty times.
   */
  year: string;
  /** Plain-language line naming where the entry came from. */
  provenance: string;
  /** Optional inline link to the original artefact. */
  link?: string;
  /** Photograph filed with the entry, shown inline at reading width. */
  image?: { src: string; alt: string };
  messages: Message[];
  /** Drives the "needs you" count in the header; not shown on the entry. */
  open?: boolean;
};

export const entries: Entry[] = [
  {
    id: "rash",
    kind: "symptom",
    qualifier: "New",
    headline: "New rash on the neck",
    summary:
      "Red, slightly raised patch on the left side of the neck, noticed at breakfast. Nothing new in her diet; GP phone review booked for Monday morning.",
    people: ["yuval", "penny", "liz"],
    where: "Alden family",
    at: "08:12",
    date: "Sat 25 Jul",
    year: "2026",
    provenance: "from Yuval's WhatsApp · 08:12",
    link: "View photo",
    image: {
      src: "/rash-photo.png",
      alt: "Close photograph of a red, slightly raised rash on pale skin",
    },
    messages: [
      {
        from: "yuval",
        at: "08:12",
        text: "morning — she's got a red patch on her neck this morning, wasn't there yesterday. not itching her but it looks angry. photo attached",
        attachment: {
          kind: "photo",
          label: "1 photo",
          note: "Hidden until opened",
          action: "View",
        },
      },
    ],
    open: true,
  },
  {
    id: "ramipril",
    kind: "medication",
    qualifier: "Changed",
    headline: "Ramipril reduced 5 mg → 2.5 mg",
    summary:
      "Blood pressure running low on morning readings. Same time each morning, half the dose — review again in four weeks.",
    people: ["liz", "penny", "owen", "yuval"],
    where: "Alden family",
    at: "11:30",
    date: "Fri 24 Jul",
    year: "2026",
    provenance: "from Dr Chen's letter, filed by Liz",
    link: "View letter",
    messages: [
      {
        from: "liz",
        at: "11:30",
        text: "letter came from Dr Chen — he's halving the ramipril to 2.5mg, same time each morning. review in 4 weeks. scanning it now",
        attachment: {
          kind: "document",
          label: "Letter · 2 pages",
          note: "Filed to Documents",
          action: "Open",
        },
      },
    ],
  },
  {
    id: "nurse",
    kind: "appointment",
    headline: "District nurse booked — Fri 31 Jul, 10:00",
    summary:
      "She'll do the blood pressure check while she's there, so no separate surgery trip is needed.",
    people: ["owen", "yuval"],
    where: "Alden family",
    at: "18:22",
    date: "Thu 23 Jul",
    year: "2026",
    provenance: "from Owen's message on the family chat",
    messages: [
      {
        from: "owen",
        at: "18:22",
        text: "got the district nurse for fri 31st at 10. she'll do the BP check at the same time so mum doesn't need to go in",
      },
    ],
  },
  {
    id: "garden",
    kind: "wellbeing",
    headline: "An hour in the garden after lunch",
    summary:
      "Sat out the back for a good hour and named every one of the roses. A good day, worth recording too.",
    people: ["matthew"],
    where: "Voice note",
    at: "15:06",
    date: "Wed 22 Jul",
    year: "2026",
    provenance: "from Matthew's voice note · 0:29",
    messages: [
      {
        from: "matthew",
        at: "15:06",
        voice: {
          duration: "0:29",
          transcript:
            "…she's been out the back the whole hour, went round and named every rose. Proper good afternoon, thought you'd want that one written down too.",
        },
      },
    ],
  },
  {
    id: "allowance",
    kind: "admin",
    headline: "Attendance Allowance renewal sent",
    summary:
      "Section 1 filled in by Yuval, day-to-day care details by Liz. Posted first class; copy filed to Documents.",
    people: ["yuval", "liz"],
    where: "Alden family",
    at: "16:40",
    date: "Mon 20 Jul",
    year: "2026",
    provenance: "from Yuval's scan",
    link: "View form",
    messages: [
      {
        from: "yuval",
        at: "16:40",
        text: "renewal's gone in the post today, first class. scanned a copy before it went",
        attachment: {
          kind: "document",
          label: "Form · 14 pages",
          note: "Filed to Documents",
          action: "Open",
        },
      },
    ],
  },
  {
    id: "dizzy",
    kind: "symptom",
    headline: "Dizzy standing up from the armchair",
    summary:
      "Twice in one afternoon, both times steadying herself on the arm. No fall. Penny flagged it against the morning blood pressure readings.",
    people: ["yuval", "penny"],
    where: "Alden family",
    at: "16:55",
    date: "Fri 17 Jul",
    year: "2026",
    provenance: "from Yuval's WhatsApp · 16:55",
    messages: [
      {
        from: "yuval",
        at: "16:55",
        text: "she went a bit swimmy standing up from the chair this afternoon, twice. held on and was fine after a few seconds. no fall",
      },
      {
        from: "penny",
        outbound: true,
        at: "16:58",
        text: "Noted. Her morning readings have been on the low side all week — I'll put both in front of Dr Chen.",
      },
    ],
  },
  {
    id: "bp-low",
    kind: "symptom",
    qualifier: "Watch",
    headline: "Morning blood pressure 98/56",
    summary:
      "Lowest of the week, and the fourth morning under 105 systolic. Taken sitting, same cuff, same time.",
    people: ["liz", "penny"],
    where: "Alden family",
    at: "08:20",
    date: "Wed 15 Jul",
    year: "2026",
    provenance: "from Liz's WhatsApp · 08:20",
    messages: [
      {
        from: "liz",
        at: "08:20",
        text: "98/56 this morning. that's four days now under 105. she says she feels fine but it's lower than it was",
      },
    ],
  },
  {
    id: "bloods",
    kind: "appointment",
    headline: "Bloods at the surgery — kidney function",
    summary:
      "Routine monitoring for the ramipril and furosemide. Owen drove; in and out in twenty minutes.",
    people: ["owen"],
    where: "Alden family",
    at: "09:40",
    date: "Mon 13 Jul",
    year: "2026",
    provenance: "from Owen's message on the family chat",
    messages: [
      {
        from: "owen",
        at: "09:40",
        text: "bloods done, U&Es for the tablets. twenty minutes door to door, she was pleased with that",
      },
    ],
  },
  {
    id: "weight",
    kind: "symptom",
    qualifier: "Watch",
    headline: "Weight up 1.6 kg in four days",
    summary:
      "Ankles puffier by the evening. Called the surgery; advised an extra furosemide for three days and to weigh daily.",
    people: ["yuval", "liz", "penny"],
    where: "Alden family",
    at: "07:50",
    date: "Wed 8 Jul",
    year: "2026",
    provenance: "from the daily weight Penny asks for each morning",
    messages: [
      {
        from: "penny",
        outbound: true,
        at: "07:30",
        text: "Morning Yuval — today's weight when you get a moment?",
      },
      {
        from: "yuval",
        at: "07:50",
        text: "64.9. that's 1.6 up since saturday. her ankles were puffy again last night",
      },
    ],
  },
  {
    id: "furosemide-extra",
    kind: "medication",
    qualifier: "Changed",
    headline: "Extra furosemide for three days",
    summary:
      "On the surgery's advice, after the weight rise. Back to the usual 40 mg from Friday; weight to be recorded every morning meanwhile.",
    people: ["liz", "penny"],
    where: "Alden family",
    at: "11:15",
    date: "Tue 7 Jul",
    year: "2026",
    provenance: "from Liz's call with the surgery, 11:15",
    messages: [
      {
        from: "liz",
        at: "11:15",
        text: "spoke to the nurse practitioner — extra 20mg furosemide for 3 days then back to normal, and weigh her every morning",
      },
    ],
  },
  {
    id: "birthday",
    kind: "wellbeing",
    headline: "Two hours at Aidan's birthday lunch",
    summary:
      "Stayed the whole meal, knew everyone, told the story about the caravan again. Tired afterwards but in good spirits.",
    people: ["matthew", "owen"],
    where: "Alden family",
    at: "15:20",
    date: "Sat 4 Jul",
    year: "2026",
    provenance: "from Matthew's message on the family chat",
    messages: [
      {
        from: "matthew",
        at: "15:20",
        text: "she did the whole lunch, two hours, and told the caravan story to anyone who'd listen. shattered after but worth it",
      },
    ],
  },
  {
    id: "stool",
    kind: "admin",
    qualifier: "Equipment",
    headline: "Perching stool delivered",
    summary:
      "From the occupational therapy assessment in May. In the kitchen by the sink, where she was standing longest.",
    people: ["yuval"],
    where: "Alden family",
    at: "13:05",
    date: "Mon 29 Jun",
    year: "2026",
    provenance: "from Yuval's WhatsApp · 13:05",
    messages: [
      {
        from: "yuval",
        at: "13:05",
        text: "perching stool came. put it by the sink, she's already used it doing the washing up",
      },
    ],
  },
  {
    id: "near-fall",
    kind: "symptom",
    headline: "Near-fall in the bathroom overnight",
    summary:
      "Caught the door frame. No injury and she did not press the pendant. Night light now left on in the hallway.",
    people: ["yuval", "liz"],
    where: "Alden family",
    at: "06:40",
    date: "Thu 25 Jun",
    year: "2026",
    provenance: "from Yuval's WhatsApp · 06:40",
    messages: [
      {
        from: "yuval",
        at: "06:40",
        text: "she had a wobble in the bathroom in the night, grabbed the door frame. no injury but she didn't press the pendant. leaving the hall light on now",
      },
      {
        from: "liz",
        at: "07:02",
        text: "third time she's not used the pendant. we should talk to her about that again",
      },
    ],
  },
  {
    id: "echo",
    kind: "appointment",
    headline: "Echocardiogram, Croydon University Hospital",
    summary:
      "Yearly scan for the heart failure. Reported as stable, ejection fraction unchanged from last year.",
    people: ["owen", "liz"],
    where: "Alden family",
    at: "14:10",
    date: "Wed 17 Jun",
    year: "2026",
    provenance: "from Owen's message, and the report Liz filed",
    link: "View report",
    messages: [
      {
        from: "owen",
        at: "14:10",
        text: "echo done. sonographer said nothing looked different from last year, letter to follow",
      },
      {
        from: "liz",
        at: "09:15",
        text: "report's come through — stable, EF unchanged. filing it",
        attachment: {
          kind: "document",
          label: "Report · 3 pages",
          note: "Filed to Documents",
          action: "Open",
        },
      },
    ],
  },
  {
    id: "hf-review",
    kind: "appointment",
    headline: "Annual heart failure review with Dr Chen",
    summary:
      "Ramipril increased to 5 mg at this visit, on the blood pressure readings at the time. Fluid advice unchanged; weigh daily, report a 2 kg rise.",
    people: ["liz", "owen"],
    where: "Alden family",
    at: "11:45",
    date: "Fri 5 Jun",
    year: "2026",
    provenance: "from Liz's notes after the appointment",
    messages: [
      {
        from: "liz",
        at: "11:45",
        text: "review went well. he's putting the ramipril up to 5mg, everything else stays. weigh daily and ring if she's up 2kg",
      },
    ],
  },
  {
    id: "knees",
    kind: "symptom",
    headline: "Knees worse through the wet fortnight",
    summary:
      "Stairs taking noticeably longer and she is using the frame indoors more. Paracetamol regularly rather than as needed.",
    people: ["matthew", "yuval"],
    where: "Alden family",
    at: "19:30",
    date: "Wed 20 May",
    year: "2026",
    provenance: "from Matthew's message on the family chat",
    messages: [
      {
        from: "matthew",
        at: "19:30",
        text: "knees have been bad all fortnight with the weather. she's on the paracetamol properly now not just when it's bad, and using the frame inside",
      },
    ],
  },
  {
    id: "ot",
    kind: "appointment",
    headline: "Occupational therapy home assessment",
    summary:
      "Recommended a perching stool for the kitchen and a second stair rail. Bathroom judged adequate with the existing grab rail.",
    people: ["yuval", "liz"],
    where: "Alden family",
    at: "10:30",
    date: "Thu 14 May",
    year: "2026",
    provenance: "from Yuval's message on the family chat",
    messages: [
      {
        from: "yuval",
        at: "10:30",
        text: "OT's been. perching stool for the kitchen and a second rail on the stairs. she said the bathroom's fine as it is with the grab rail",
      },
    ],
  },
  {
    id: "blue-badge",
    kind: "admin",
    headline: "Blue Badge renewed for three years",
    summary:
      "Renewed online with the existing photograph. New badge arrived within the fortnight; old one destroyed.",
    people: ["owen"],
    where: "Alden family",
    at: "17:20",
    date: "Fri 8 May",
    year: "2026",
    provenance: "from Owen's message on the family chat",
    messages: [
      {
        from: "owen",
        at: "17:20",
        text: "blue badge renewed, three more years. used the same photo. cut up the old one when the new one came",
      },
    ],
  },
  {
    id: "shingles",
    kind: "appointment",
    headline: "Shingles vaccination at the surgery",
    summary:
      "First of two doses. Sore arm for a day and nothing else; second dose due in the autumn.",
    people: ["liz"],
    where: "Alden family",
    at: "15:00",
    date: "Wed 15 Apr",
    year: "2026",
    provenance: "from Liz's message on the family chat",
    messages: [
      {
        from: "liz",
        at: "15:00",
        text: "shingles jab done, first of two. arm's a bit sore, nothing else. second one in the autumn",
      },
    ],
  },
  {
    id: "chest",
    kind: "medication",
    headline: "Amoxicillin, five days, for a chest infection",
    summary:
      "Productive cough and a temperature over a weekend. Finished the course; cough settled within the fortnight and no hospital admission.",
    people: ["yuval", "liz", "penny"],
    where: "Alden family",
    at: "09:10",
    date: "Tue 3 Mar",
    year: "2026",
    provenance: "from the out-of-hours note, filed by Liz",
    messages: [
      {
        from: "yuval",
        at: "21:40",
        text: "she's been coughing all weekend and she's warm. ringing 111",
      },
      {
        from: "liz",
        at: "09:10",
        text: "out of hours saw her, chest infection, amoxicillin for 5 days. no admission thank god",
      },
    ],
  },
  {
    id: "downstairs",
    kind: "wellbeing",
    headline: "Bed moved down to the front room",
    summary:
      "Her decision, after a winter of finding the stairs harder. The front room gets the morning light and she is close to the kitchen.",
    people: ["matthew", "owen", "liz"],
    where: "Alden family",
    at: "16:15",
    date: "Thu 12 Feb",
    year: "2026",
    provenance: "from Matthew's message on the family chat",
    messages: [
      {
        from: "matthew",
        at: "16:15",
        text: "bed's downstairs in the front room now. her idea, not ours — said she was tired of the stairs. gets the morning sun in there anyway",
      },
    ],
  },
  {
    id: "pill-organiser",
    kind: "admin",
    headline: "Weekly pill organiser, and Penny's reminders started",
    summary:
      "Pharmacy now dispenses a weekly blister pack. Penny began asking the person on duty to confirm the morning and evening doses.",
    people: ["liz", "penny"],
    where: "Alden family",
    at: "12:00",
    date: "Wed 14 Jan",
    year: "2026",
    provenance: "from Liz's message on the family chat",
    messages: [
      {
        from: "liz",
        at: "12:00",
        text: "pharmacy's doing a weekly blister pack from now on. penny, can you start checking she's taken them morning and night?",
      },
      {
        from: "penny",
        outbound: true,
        at: "12:04",
        text: "Yes — I'll ask whoever is on duty at 08:00 and 20:00 and file the answer here.",
      },
    ],
  },
  {
    id: "boosters",
    kind: "appointment",
    headline: "Flu and COVID boosters",
    summary:
      "Both at the surgery on the same morning. Quiet day afterwards, no reaction beyond a sore arm.",
    people: ["owen"],
    where: "Alden family",
    at: "10:50",
    date: "Thu 4 Dec",
    year: "2025",
    provenance: "from Owen's message on the family chat",
    messages: [
      {
        from: "owen",
        at: "10:50",
        text: "flu and covid both done this morning. sore arm, quiet afternoon, otherwise nothing",
      },
    ],
  },
  {
    id: "fall-garden",
    kind: "symptom",
    qualifier: "Serious",
    headline: "Fall in the garden — A&E, no fracture",
    summary:
      "Tripped on the uneven slab by the shed. Six hours in A&E; X-ray clear, extensive bruising to the left hip. The slab was relaid the following week.",
    people: ["yuval", "liz", "owen", "matthew"],
    where: "Alden family",
    at: "14:25",
    date: "Sat 15 Nov",
    year: "2025",
    provenance: "from Yuval's WhatsApp, and the discharge letter Liz filed",
    link: "View discharge letter",
    messages: [
      {
        from: "yuval",
        at: "14:25",
        text: "she's had a fall in the garden, that slab by the shed. she's conscious and talking but she can't put weight on the left side. calling an ambulance",
      },
      {
        from: "liz",
        at: "22:10",
        text: "home. six hours but the x-ray's clear, no fracture. badly bruised hip. discharge letter attached",
        attachment: {
          kind: "document",
          label: "Discharge letter · 2 pages",
          note: "Filed to Documents",
          action: "Open",
        },
      },
    ],
  },
  {
    id: "furosemide-start",
    kind: "medication",
    qualifier: "Changed",
    headline: "Furosemide increased to 40 mg",
    summary:
      "After a run of swollen ankles through the autumn. Taken in the mornings so it does not disturb the night.",
    people: ["liz"],
    where: "Alden family",
    at: "16:30",
    date: "Tue 21 Oct",
    year: "2025",
    provenance: "from Dr Chen's letter, filed by Liz",
    messages: [
      {
        from: "liz",
        at: "16:30",
        text: "furosemide going up to 40mg for the ankles. mornings only so she's not up in the night",
      },
    ],
  },
  {
    id: "knee-injection",
    kind: "appointment",
    headline: "Steroid injection, right knee",
    summary:
      "Third injection in two years. Good relief for about ten weeks, then a gradual return of the pain.",
    people: ["matthew"],
    where: "Alden family",
    at: "11:20",
    date: "Wed 3 Sep",
    year: "2025",
    provenance: "from Matthew's message on the family chat",
    messages: [
      {
        from: "matthew",
        at: "11:20",
        text: "knee injection done, number three. last one gave her a good couple of months before it wore off",
      },
    ],
  },
  {
    id: "driving",
    kind: "wellbeing",
    headline: "Gave up driving",
    summary:
      "Her own decision after a near miss at the Brighton Road junction. She sold the car to a neighbour and has kept the keys to the garage.",
    people: ["owen", "liz", "matthew"],
    where: "Alden family",
    at: "18:45",
    date: "Fri 8 Aug",
    year: "2025",
    provenance: "from Owen's message on the family chat",
    messages: [
      {
        from: "owen",
        at: "18:45",
        text: "she's decided to stop driving. had a near miss at the brighton road junction and said that was that. selling the car to next door",
      },
      {
        from: "liz",
        at: "19:02",
        text: "that can't have been easy for her. we should make sure someone's offering lifts, not waiting to be asked",
      },
    ],
  },
];

export const medications = [
  { name: "Ramipril 2.5 mg", detail: "Dose lowered Mon 20 Jul", changed: true },
  { name: "Furosemide 40 mg", detail: "Mornings · since Nov 2023", changed: false },
  { name: "Atorvastatin 20 mg", detail: "Nightly · since 2017", changed: false },
];

export const reminders = [
  {
    time: "08:00",
    title: "Ramipril 2.5 mg · Furosemide 40 mg",
    note: "Penny asked Yuval on WhatsApp — “Has she taken her morning tablets?” · replied 08:04",
    state: "taken",
    done: true,
  },
  {
    time: "20:00",
    title: "Atorvastatin 20 mg",
    note: "Penny will ask Liz on WhatsApp at 20:00",
    state: "tonight",
    done: false,
  },
];

export const conditions = [
  { name: "Heart failure", detail: "Diagnosed Nov 2023 · yearly echo · daily fluid watch" },
  { name: "Hypertension", detail: "Since 2014 · reviewed Jul 2026" },
  { name: "Osteoarthritis, knees", detail: "Since 2019 · managed with physio" },
];

export const contributors: PersonId[] = ["yuval", "liz", "owen", "matthew"];

/* ------------------------------------------------------------ clinical summary ---- */

/**
 * What "Generate clinical summary" produces from the entries above.
 *
 * The shape deliberately mirrors the real report contract — a lead paragraph,
 * cited sections, questions, watch items and gaps — so that swapping the mock
 * for `GET /api/reports/{id}` is a change of source and not of layout.
 *
 * Two rules run through the copy. Every clinical statement names the entry it
 * came from, because a summary a GP cannot check is worth less than the chat
 * log it replaced. And where Penny has noticed a pattern it cannot interpret,
 * it says so in `gaps` rather than implying a cause — the timing of the rash
 * against the dose change is reported, never explained.
 */
export type SummarySection = {
  heading: string;
  body: string;
  /** Entry ids this section was drawn from; rendered as checkable citations. */
  cites: string[];
};

export const clinicalSummary = {
  title: "Clinical summary",
  generatedAt: "Sat 25 Jul 2026, 08:40",
  period: "Twelve months to 25 July 2026",
  /**
   * Counted, not hardcoded: the summary states how much of the record it read,
   * and a number typed by hand would quietly stop being true the next time an
   * entry was added.
   */
  entryCount: entries.filter((e) => e.year === "2026").length,
  lead:
    "A stable year on the whole, with two things live. Blood pressure has been drifting down " +
    "since the spring and the ramipril was halved on 24 July; a new rash appeared on the neck " +
    "the following morning and has not yet been assessed. Function has declined gradually — " +
    "she now sleeps downstairs and no longer drives — but she is still doing a full afternoon " +
    "out when she wants to.",
  sections: [
    {
      heading: "Background",
      body:
        "Heart failure diagnosed November 2023, hypertension since 2014, osteoarthritis of both " +
        "knees since 2019. A fall in the garden in November 2025 took her to A&E; the X-ray was " +
        "clear. She gave up driving in August 2025 and moved her bed to the front room in " +
        "February 2026, both her own decisions.",
      cites: ["fall-garden", "driving", "downstairs"],
    },
    {
      heading: "New symptom",
      body:
        "Rash on the left side of the neck, noticed at breakfast on 25 July. Red, slightly raised, " +
        "not itching her. A photograph is on file. No change of diet, soap or bedding reported. " +
        "A GP telephone review is booked for Monday morning.",
      cites: ["rash"],
    },
    {
      heading: "Medication change",
      body:
        "Ramipril reduced from 5 mg to 2.5 mg on 24 July, on Dr Chen's letter, for blood pressure " +
        "running low on morning readings. Same dosing time, half the dose, review in four weeks " +
        "(due about 21 August). Furosemide 40 mg and atorvastatin 20 mg are unchanged.",
      cites: ["ramipril"],
    },
    {
      heading: "Monitoring and appointments",
      body:
        "District nurse booked for Friday 31 July at 10:00. She will take the blood pressure at " +
        "that visit, so no separate surgery attendance is planned this month.",
      cites: ["nurse"],
    },
    {
      heading: "Function and wellbeing",
      body:
        "An hour in the garden on 22 July, and two hours at a family lunch on 4 July, sustained " +
        "and engaged throughout both. Against that, knees were worse through the wet fortnight in " +
        "May and she is using the frame indoors more. Baseline otherwise unchanged: mobile with a " +
        "frame, breathless on one flight of stairs.",
      cites: ["garden", "birthday", "knees"],
    },
    {
      heading: "Fluid balance",
      body:
        "One episode this year: weight up 1.6 kg over four days in early July with puffier ankles, " +
        "settled with three days of extra furosemide on the surgery's advice. Daily weights have " +
        "been recorded since. The yearly echocardiogram in June was reported as stable with the " +
        "ejection fraction unchanged.",
      cites: ["weight", "furosemide-extra", "echo"],
    },
    {
      heading: "Administrative",
      body:
        "Attendance Allowance renewal completed and posted first class on 20 July. A scanned copy " +
        "is filed to Documents.",
      cites: ["allowance"],
    },
  ] satisfies SummarySection[],
  questions: [
    "Is the new rash connected to the ramipril reduction on 24 July, or unrelated?",
    "Should the district nurse recheck blood pressure against the new dose on 31 July?",
    "Is a four-week medication review still right, or should it be brought forward?",
  ],
  watch: [
    "Any swelling of the face, lips or tongue, or difficulty breathing — same-day advice",
    "Whether the rash spreads, blisters or begins to itch",
    "Morning blood pressure now the dose has halved, and dizziness on standing",
  ],
  gaps: [
    "No blood pressure readings have been recorded since the dose changed on 24 July",
    "Nothing recorded about the rash since the morning it was noticed",
    "Penny has noted that the rash followed the dose change by a day, but cannot tell whether the two are connected",
  ],
};
