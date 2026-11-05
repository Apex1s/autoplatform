# Outbound playbooks

Five sequences across three segments. Every message leans on **verifiable proof**: the live demo, a response-time audit you actually ran, or the prospect's own numbers. There are no invented case studies; until you have real clients, the demo and the audit *are* the case study.

Merge fields look like `{{first_name}}`. Demo link: `https://autoplatform.data-marketplace-edge.workers.dev/demo?cta=<your booking link>`.

## Ground rules (read before sending anything)

- **Email:**
  - Send from a separate domain (e.g. `get<yourbrand>.com`) with SPF, DKIM and DMARC set.
  - Warm it for 2 weeks, then stay under 30–50 sends per inbox per day.
  - Plain text, one link at most, and a one-line opt-out in every email.
  - **EU/France:** B2B prospecting by email is allowed when the message relates to the recipient's role and they can opt out easily (CNIL).
  - **US:** CAN-SPAM requires a physical address and an unsubscribe.
  - **Morocco:** law 09-08; keep the list small, relevant and opt-out friendly.
- **LinkedIn:** send manually or semi-manually, with no more than about 20 connection requests a day. Automation tools risk the account.
- **WhatsApp:** only **1:1, manual messages to a business's publicly listed WhatsApp number**, or to people who gave you their number. Never bulk-send or use automation for cold messages. WhatsApp bans numbers that collect spam reports, and the Business API requires opt-in for business-initiated messages. Stop after one follow-up without a reply.
- **Truthfulness:** only quote response times you measured, and only claim results you can show.

## Proof assets to prepare once

1. **Live demo link** with your booking `cta`.
2. **Speed-to-lead audit:** for real-estate and clinic prospects, submit a genuine enquiry on their website or ad form during business hours. Note the time to first response (minutes or hours, or "no response after 24 h") and what they replied with.
3. **60-second Loom:** run the demo on *their* scenario (edit the payload with their business name), show the 202 and the drafted reply, and open the CRM payload.
4. **Sample weekly report:** run `usage_reporter` on the demo or a pilot client and screenshot the Slack message.

---

## Sequence 1: B2B lead-gen and marketing agencies → White-Label Engine (LinkedIn DM)

**Who:** agency owners and heads of delivery at 3–30-person agencies selling lead gen, paid ads, GoHighLevel/HubSpot builds or websites to SMBs.

**Where to find them:**
- LinkedIn search: "agency owner" + "GoHighLevel" / "lead generation" / "HubSpot partner".
- Agency directories (Clutch, the HubSpot partner directory).
- Agencies running their own Meta ads (Meta Ad Library).

**Signal to reference:** they post about client results, AI or automations; they hire "automation specialists"; their site sells "AI chatbots" or "lead nurturing".

**Day 0, connection note (max 300 characters):**
> Hi {{first_name}}, saw {{agency}} runs {{service}} for {{niche}}. I built a white-label engine agencies use to sell AI lead response (qualify + reply in seconds, with signed CRM delivery) without running any infra. Happy to connect.

**Day 1, after they accept:**
> Thanks for connecting, {{first_name}}. Quick one: when a lead from your {{niche}} campaigns hits a client's form, who replies and how fast?
>
> I run a backend agencies plug into: each client gets a branded endpoint, AI scores the lead 0–100 and drafts the WhatsApp/email reply, and it lands in the CRM in a few seconds. There's automatic Groq→Gemini failover, so an AI outage doesn't drop leads.
>
> Live demo (no signup, try it with any payload): {{demo_link}}
> Worth a 15-min look at how you'd resell it at $300–800/client?

**Day 4, if no reply:**
> One number that might matter: at 5 clients, the engine is $500/mo flat for you (25 client endpoints, 5k jobs each), and you set the client price. Per-client usage and ROI reports come out of an API you can put on your own dashboards. Want me to spin up a test sub-client with your agency name so you can try it on a real campaign?

**Day 10, break-up:**
> Last nudge from me. If AI lead response isn't on your roadmap this quarter, no worries. If it is, reply "sandbox" and I'll send a sub-client key you can wire into one client's form today.

**Call (15 minutes):**
1. Their current stack (Zapier/Make/GHL workflows) and what breaks.
2. Live demo with the "Simulate a Groq outage" box ticked.
3. Show `GET /agency/usage` output.
4. Offer a **14-day sandbox sub-client**, then $500/mo.

**Objections:**
- *"We already use Make/Zapier."* Keep it. Our signed webhook drops straight into a Make/Zapier "catch hook"; we replace the fragile AI and retry part, not their whole stack.
- *"Data privacy?"* Per-client keys, HMAC-signed deliveries, PII-sensitive clients can be pinned to one provider, and you sign a DPA with us.

## Sequence 2: Agencies → White-Label Engine (cold email)

**Subject options:** `AI lead response for {{agency}}'s clients` · `{{agency}} + white-label AI ops` · `question about {{client_vertical}} leads`

**Email 1, day 0:**
> Hi {{first_name}},
>
> {{agency}} gets {{niche}} clients leads; the gap I keep seeing is what happens in the first 5 minutes after the lead arrives.
>
> I run a white-label backend agencies resell as "AI lead response":
> - Each client gets a branded webhook (`/webhook/{{agency}}-client/lead_qualifier`).
> - The AI extracts the details, scores the lead 0–100 and drafts the WhatsApp/email reply.
> - Signed delivery into the CRM in seconds; if Groq rate-limits, Gemini takes over automatically.
>
> Try it live with any lead payload: {{demo_link}}
>
> $500/mo flat for up to 25 clients. Open to a 15-min look?
>
> {{signature}}
> (Not relevant? Reply "no" and I won't follow up.)

**Email 2, day 3:** reply in the same thread.
> {{first_name}}, the part agencies usually care about is reporting. Every Monday each client's Slack gets a report: leads processed, hot leads, median time-to-reply, AI outages auto-recovered, hours saved. It's the thing that keeps the retainer alive. Want me to generate one on a test sub-client branded {{agency}}?

**Email 3, day 7:**
> Quick math for {{agency}}: if 5 clients pay you $400/mo for AI lead response, that's $2,000/mo revenue on a $500/mo cost, with no servers, queues or LLM accounts on your side. I can set up the first client with you in a 30-min call.

**Email 4, day 14, break-up:**
> Closing the loop. If timing's off, I'll leave it here. The demo stays up at {{demo_link}} if you want to show a client.

## Sequence 3: Real estate agencies and developers → Speed-to-Lead ($1,500 + $1,000/mo)

**Who:** agencies and developers running Meta or Google lead ads or listing portals, with at least 300 leads a month. Signal: active ads in Meta Ad Library, a lead form on the site, or "contactez-nous" forms.

**Before day 0:** run the **speed-to-lead audit** on their form and record the real response time.

**Email 1 (EN):**
> Subject: I enquired about {{property_type}} in {{area}} on {{date}}
>
> Hi {{first_name}},
>
> On {{date}} at {{time}} I filled in the enquiry form on {{website}} (a genuine test, 3-bedroom in {{area}}). {{audit_result: "The first reply came {{X}} hours later" | "I haven't had a reply yet"}}.
>
> Leads that hear back within minutes are far likelier to book a visit than leads contacted hours later; you've probably seen it in your own numbers.
>
> I set up systems that answer in seconds: the AI reads the enquiry, scores budget and timeline, drafts a personal WhatsApp reply with your visit-booking link, and pushes the hot ones to your CRM and your phone. Here's the same flow on a sample lead: {{demo_link}}
>
> Worth 15 minutes to see it on your own form?
>
> {{signature}}
> (Reply "stop" and I won't write again.)

**Email 1 (FR):**
> Objet : J'ai demandé des infos sur un {{property_type}} à {{area}} le {{date}}
>
> Bonjour {{first_name}},
>
> Le {{date}} à {{time}}, j'ai rempli le formulaire de contact de {{website}} (test réel : un 3 pièces à {{area}}). {{audit_result: "La première réponse est arrivée {{X}} heures plus tard" | "Je n'ai pas encore reçu de réponse"}}.
>
> Un prospect recontacté en quelques minutes réserve bien plus souvent une visite qu'un prospect rappelé des heures après ; vous le voyez sûrement dans vos chiffres.
>
> Je mets en place des systèmes qui répondent en quelques secondes. L'IA lit la demande, note le budget et le délai, et prépare un message WhatsApp personnalisé avec votre lien de prise de rendez-vous. Les prospects chauds arrivent dans votre CRM et sur votre téléphone. Démo en direct sur un exemple : {{demo_link}}
>
> 15 minutes pour le voir sur votre propre formulaire ?
>
> {{signature}}
> (Répondez « stop » et je ne vous écrirai plus.)

**LinkedIn, day 2** (to the founder or sales manager): *"Sent you a note about the test enquiry I made on {{website}}. Happy to show the 5-second version live if useful."*

**Email 2, day 4:**
> One detail I left out: you approve every AI reply for the first week, and nothing goes to a lead without your rules. Most agencies switch hot leads to auto-send after they see the drafts. Want to see 20 of your past leads re-scored? Send me an export and I'll run them.

**Email 3, day 9:** offer the **re-score of 20 historical leads** as a free deliverable (anonymised CSV in, a scored sheet and drafted replies out). It's the strongest step to the close.

**Call script (20 minutes):**
1. Replay the audit result.
2. Run the demo with *their* listing.
3. Show the CRM payload and the Monday report.
4. Pricing: $1,500 setup + $1,000/mo, 3-month minimum.
5. Close on a start date.

**Objection:** *"Too expensive."* Ask how much one extra sale is worth to them in commission, then ask how many leads a month currently go unanswered for over an hour. Let them do the math.

## Sequence 4: High-ticket clinics (dental implants, aesthetics, hair transplant, IVF) → Speed-to-Lead

**Who:** clinic owners and marketing managers with ad-driven consultations, typically with multiple locations or a coordinator team. Signal: before/after ads, WhatsApp click-to-chat buttons, "free consultation" forms.

**Before day 0:** run the audit as in Sequence 3, plus one after-hours enquiry (8–10 pm), which is when most clinics drop leads.

**Email 1 (EN):**
> Subject: your consultation form at 9 pm
>
> Hi Dr {{last_name}},
>
> I submitted a consultation request on {{website}} on {{date}} at 21:{{min}} (genuine test). {{audit_result}}.
>
> For treatments at {{price_band}}, the first clinic to answer usually books the consultation. I run a system that replies in seconds, 24/7:
> - The AI reads the request and detects the treatment, urgency and budget signals.
> - It drafts a warm WhatsApp reply with your booking link.
> - Your coordinator gets the hot ones with a score.
>
> Medical claims stay out of AI replies (you set the rules), and patient data is handled under a DPA.
>
> 2-minute demo: {{demo_link}}. 15 minutes this week to see it on your form?

**Email 1 (FR):**
> Objet : votre formulaire de consultation à 21h
>
> Bonjour Docteur {{last_name}},
>
> J'ai envoyé une demande de consultation sur {{website}} le {{date}} à 21h{{min}} (test réel). {{audit_result}}.
>
> Pour des soins à {{price_band}}, c'est souvent la première clinique qui répond qui obtient la consultation. Mon système répond en quelques secondes, 24h/24 :
> - L'IA lit la demande et repère le soin, l'urgence et le budget.
> - Elle prépare une réponse WhatsApp chaleureuse avec votre lien de rendez-vous.
> - Votre coordinatrice reçoit les demandes chaudes avec un score.
>
> Aucune allégation médicale dans les réponses (vous fixez les règles) ; données patients encadrées par un contrat de sous-traitance.
>
> Démo de 2 minutes : {{demo_link}}. 15 minutes cette semaine pour le voir sur votre formulaire ?

**Phone follow-up, day 3:** call the front desk and ask for the person who handles online enquiries: *"I sent Dr {{last_name}} the result of a test enquiry I made on your site. Who looks after the consultation requests?"* Then email that person directly.

**Email 2, day 6:** offer the **after-hours report**: *"I can show you exactly what a patient who wrote to you last night would have received at 21:02 instead of the next morning."* Run the demo with their treatment.

**Compliance notes for clinics:** keep the AI to logistics (booking, pricing ranges *they* provide, next steps), never medical advice. Pin these clients to `"llm_providers": ["groq"]` until you're on a paid Gemini tier, and sign a DPA.

## Sequence 5: Regional e-commerce, restaurants and hospitality → WhatsApp order routing (WhatsApp-first, manual)

**Who:** restaurants, dark kitchens, bakeries, florists, boutique hotels and Instagram/WhatsApp sellers taking **20+ orders a day on WhatsApp**. Signal: "commandez sur WhatsApp" in the Instagram bio, a WhatsApp Business catalog, or Glovo/Jumia listings plus a direct WhatsApp number.

**Channel rule:** message the **business's public WhatsApp number**, 1:1, manually, during quiet hours (15:00–17:00 for restaurants). Or walk in. One follow-up maximum.

**Message 1 (FR/Darija-friendly), with a 30-second screen recording of the demo "WhatsApp order" scenario using their menu items:**
> Salam {{first_name}} 👋 je suis {{your_name}}, je travaille sur l'automatisation des commandes WhatsApp pour les restos à {{city}}.
>
> J'ai fait un test avec 3 plats de votre carte : le client écrit « bghit 2 {{item1}} w 1 {{item2}}, livraison {{quartier}} », et le système comprend tout (même en darija). Il calcule le total avec VOS prix, prépare la confirmation et envoie la commande en cuisine. Ça prend 3 secondes : {{short_video_or_demo_link}}
>
> Si ça vous intéresse, je passe vous le montrer 10 minutes. Sinon pas de souci, bonne journée !

**Message 1 (EN):**
> Hi {{first_name}}, I'm {{your_name}}. I automate WhatsApp order-taking for restaurants in {{city}}. I tested it with 3 items from your menu: a customer writes "2 {{item1}} and 1 {{item2}}, deliver to {{area}}", and it reads the order (any language), prices it from YOUR menu, drafts the confirmation and sends it to the kitchen in about 3 seconds: {{link}}. Happy to show you in 10 minutes. If not relevant, no worries!

**Follow-up, day 3 (only if the message was read):**
> Petite précision : les prix viennent toujours de votre carte, jamais de l'IA, et rien n'est confirmé au client sans votre règle. On peut commencer par 1 semaine de test sur vos vrais messages.

**In-person (highest conversion for restaurants):** show the demo on your phone with *their* menu loaded, during a quiet hour.

**Offer:** the same $1,500 + $1,000/mo retainer for multi-location or high-volume shops. For single shops, propose a $750 setup + $400/mo variant (limit 1 automation, 5,000 jobs) under the `retainer` plan with `monthly_job_limit` set accordingly.

**Technical prerequisite:** auto-reading WhatsApp messages needs the **WhatsApp Business Platform (Cloud API)**, directly or via a BSP such as 360dialog or Twilio, which brings Meta per-conversation fees paid by the client. Without it, start with web or Instagram order forms posting to `/webhook/<slug>/whatsapp_order_triage`, and send the confirmation text manually.

---

## Weekly operating rhythm (to hit the funnel numbers in `offer_architecture.md`)

| Day | Activity | Volume |
|---|---|---|
| Mon | Build lists (Ad Library, Maps, LinkedIn), run 10 speed-to-lead audits | 60 contacts |
| Tue–Thu | Send sequences 1–4 (email + LinkedIn); restaurant visits/WhatsApp (seq. 5) | 40–60 touches/day |
| Fri | Demos and follow-ups; update the KPI sheet | — |

**Track weekly:** contacts → replies → demos booked → demos held → proposals → closed, by sequence. Kill or rewrite any sequence under a 2% positive reply rate after 150 sends.
