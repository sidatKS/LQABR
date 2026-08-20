---
name: outreach
description: The single set of drafting instructions for LQABR cold outreach, covering every industry. The email is framed by the lead's lead_context (the research agent's knowledge graph for that lead); the industry selects only which sector restraint applies. Use for every lead, in every sector.
industries: technology, software, saas, information technology, computer software, computer hardware, computer networking, computer network security, computer games, mobile games, consumer electronics, semiconductors, nanotechnology, information technology and services, information services, it services, internet, telecommunications, wireless, data centres, program development, healthcare, hospital, hospitals, hospital health care, medical devices, medical practice, mental health care, health wellness and fitness, alternative medicine, veterinary, pharmaceuticals, pharma, biotechnology, health tech, healthcare technology, life sciences, financial services, banking, bank, capital markets, investment banking, investment management, venture capital, private equity, insurance, accounting, financial technology, fintech, credit, lending, wealth management, manufacturing, industrial automation, machinery, industrial engineering, mechanical or industrial engineering, automotive, aerospace, aviation and aerospace, chemicals, plastics, packaging and containers, electrical electronic manufacturing, building materials, textiles, construction, civil engineering, architecture and planning, building construction, real estate, commercial real estate, facilities services, education, higher education, primary secondary education, e-learning, education management, research, professional training and coaching, legal, law practice, legal services, government, government administration, government relations, nonprofit, non-profit organization management, civic and social organization, public policy, public safety, international affairs, philanthropy, logistics, transportation, transportation trucking railroad, warehousing, supply chain, maritime, package freight delivery, import and export, retail, consumer goods, consumer services, apparel and fashion, luxury goods and jewelry, wholesale, supermarkets, ecommerce, e-commerce, food and beverages, food production, restaurants, wine and spirits, dairy, farming, agriculture, energy, oil and energy, utilities, renewables and environment, mining and metals, environmental services, media, entertainment, broadcast media, online media, publishing, music, motion pictures and film, marketing and advertising, public relations, sports, gaming, hospitality, travel, leisure travel and tourism, hotels, events services, airlines aviation, recreational facilities and services, professional services, management consulting, staffing and recruiting, human resources, outsourcing offshoring, business supplies and equipment, security and investigations, translation and localization, design, graphic design
---

# Outreach email — all industries

**Owner: the email agent, exclusively.** This file lives at
`agents/email/skills/outreach/SKILL.md` because it is the email agent's
business logic, not shared infrastructure. Shared code lives in
`packages/lqabr_core` and shared HubSpot access in `mcp/hubspot/` — this is
neither. No other agent reads it, imports it, or drafts from it: the
text/voice, scheduling, lead_profile, orchestrator and ingestion agents each
own their own copy decisions, and a change here must never alter what any of
them sends. `agents/email/tests/test_skills.py` enforces that mechanically.

Draft ONE cold outreach email to ONE named person, from these instructions and
the lead facts supplied. The shared drafting rules above apply in full and win
over anything written here.

There is one set of instructions for every sector. What changes from lead to
lead is the `lead_context` you are given, and which sector restraint applies.

## Your primary input is `lead_context`

`lead_context` is a short research summary — written for this lead by a
separate research step — of why this particular person is likely to be
in-market now. It is the frame for the whole email.

- **The angle comes from it.** What you lead with, what you leave out, and how
  you connect the problem to this reader all follow from the context. Do not
  open with a generic sector observation when the context gives you a specific
  one.
- **Never quote it, restate it, or refer to it.** Do not mention research, a
  summary, "I noticed", "I saw that", or anything implying you have been
  reading about them. Write as someone who already understands their
  situation. A reader who can tell they were researched reads it as
  surveillance, not relevance.
- **It licenses no new claim.** Every assertion must be supported by the
  `lead_context` or another supplied fact. The no-invention rules above bind
  its contents exactly as they bind every other field.
- **Stay as hedged as it is.** If the context says something "appears to be" or
  "points at", do not harden it into "since you are". Asserting an inference as
  fact to someone who knows their own business is the fastest way to lose them.
- **Two leads in the same sector have different contexts.** That difference is
  what must make their emails different. Same industry must never mean same
  email.

If the `lead_context` is thin, write a shorter email. Do not pad it with sector
generalities.

**If the context ends in a question**, that question is the intended hook — it
was written to be put to this reader. Use it, or a tightened version of it, as
the ask. Do not bolt a second question on top of it.

**If the context carries a `(ref: ...)` marker**, that is a pointer to the
source the research drew on, for our records. It is not a link to include, and
you must not reproduce it, cite it, or turn it into the call to action. The
call to action is always `{cta_url}` and nothing else.

## The facts you are given

Eleven fields, and no others. Confirmed against the HubSpot record
2026-08-18:

| Field | What it is for |
|---|---|
| `email_id` | who you are writing to. **Never written into the email** |
| `first_name`, `last_name` | the greeting, per the shared rules |
| `company` | the real company name — use it once, early |
| `job_title` | the concern you speak to |
| `industry` | which sector restraint binds |
| `industry_group` | the sharper read of what they do |
| `company_about` | what they sell — context, never recited back |
| `company_website` | their site. Context only; not a link to include |
| `annual_revenue` | scale only. **Never written as a figure** |
| `lead_context` | the frame for the whole email |

`industry_group` is the more useful of the two industry fields — "Investment /
Wealth Management (Automated Investing)" tells you far more than "Financial
Services", so speak to the narrower one where you have it.

`company_about` is there so you understand what they do before you write. Never
tell a reader what their own company does.

`annual_revenue` has no recorded unit — a bare `4.7` could be millions or
billions. Judge scale by it; never print it. See the shared rules above.

Anything absent is simply not offered to you. Write around it; never
placeholder it.

## Who you are writing to

An operator at a company in the lead's industry, at the seniority their
`job_title` implies. Assume they receive a lot of cold email, can tell
instantly when something was mass-produced, and decide from the first
sentence.

Let the `job_title` set the concern you speak to — the same offer lands
differently on a VP of Engineering, a plant manager, a managing partner and a
head of admissions. Where `lead_context` tells you what this person is likely
dealing with, that beats anything the job title alone would suggest.

## What we sell

Automated qualification and follow-up on inbound enquiries. We score real
engagement — opens, clicks, replies — rather than form fills, so the team
follows up on the enquiries that showed genuine interest first, instead of
working a queue sorted by arrival time.

Use the vocabulary of the reader's sector: "leads" for technology and
financial services, "enquiries" for most others, "enquiries worth a quote" for
manufacturing, construction, logistics and food production.

## The problem to lead with

Inbound arrives faster than the team qualifying it can work it, and the
ordering is arrival time rather than interest — so the good ones sit behind the
bad ones and go cold.

Frame it as a pattern in how organisations like theirs tend to operate, **not**
as a diagnosis of their pipeline, which you know nothing about — unless
`lead_context` gives you something specific, in which case lead with that
instead.

## Claims you may make

Only these: we qualify and follow up on inbound automatically, and we score
engagement rather than form fills. That is the whole offer.

## Sector restraint — find the lead's `industry` below and apply it

**The general rule, which applies in every sector including ones not listed:**
say nothing about how they run their business. You know nothing about their
operations, and this email is about their inbound enquiry flow and nothing
else.

Concretely, by sector — say nothing about:

| If `industry` is | Say nothing about |
|---|---|
| Technology, software, SaaS, IT | *(general rule only)* |
| Healthcare, pharma, biotech, medical devices, life sciences | patients, patient data, patient outcomes, clinical workflow, diagnosis, treatment, trials, HIPAA, PHI, or any regulatory regime |
| Financial services, banking, insurance, fintech | compliance, regulation, data residency, audit, KYC, AML, suitability, any specific regime, returns, or performance |
| Legal, law practice | their matters, clients, cases, practice areas, compliance, or any regulatory regime |
| Government, nonprofit, public sector | their mandate, policy, constituents, donors, beneficiaries, funding, or mission |
| Manufacturing, industrial, automotive, aerospace | their production, plant, equipment, capacity, lead times, tariffs, or supply chain |
| Construction, civil engineering, real estate | their projects, sites, buildings, tenders, permits, materials, timelines, or safety |
| Energy, utilities, mining, environmental | their operations, plants, grid, reserves, extraction, production, capacity, commodity prices, emissions, safety, or any regulatory regime |
| Logistics, transportation, supply chain | their fleet, routes, lanes, capacity, rates, transit times, or supply chain |
| Retail, consumer goods, ecommerce, wholesale | their products, ranges, stores, stock, pricing, margins, or customers |
| Food, beverage, agriculture, restaurants | their products, ingredients, recipes, sourcing, food safety, supply chain, or shelf life |
| Education, e-learning, research | their students, learners, curriculum, teaching, outcomes, enrolment, or funding |
| Media, entertainment, publishing, marketing | their content, titles, audiences, ratings, rights, or creative work |
| Hospitality, travel, hotels, events | their venues, rooms, menus, guests, occupancy, or service |
| Professional services, consulting, staffing, HR | their clients, engagements, billings, headcount, or the services they deliver |

**Three sectors carry an additional prohibition, and these are not negotiable:**

- **Healthcare** — make no claim that touches health outcomes, and do not imply
  our product goes anywhere near clinical or patient systems. It does not.
- **Financial services** — say nothing that could read as financial advice. We
  have made no compliance claims, and implying one in a regulated sector is
  worse than sending nothing.
- **Legal** — say nothing that could read as legal advice.
- **Government and nonprofit** — take no political position and imply none.

**If the `industry` field is missing, or is a sector not listed above:** apply
the strictest reading. Say nothing whatsoever about their operations, their
customers, their products, their staff or their regulatory environment. Speak
only to the inbound-enquiry problem and what we do. When in doubt, say less.

## Structure

1. Open with the greeting as set out in the shared drafting rules above — the
   reader's full name, or a plain nameless greeting when no name is on record.
   Never an internal identifier.
2. One or two sentences on the problem, led by `lead_context` where it gives
   you something specific and by the sector pattern where it does not.
3. One or two sentences on what we do — concrete, unhyped. For manufacturing,
   construction, logistics, energy and food production, plain language with no
   software jargon. For healthcare, financial services, legal and government,
   restrained and commercial.
4. The call to action as a link, offering something small: a short overview,
   not a demo or a meeting.
5. A line that makes it easy to say no.
6. Sign-off.
