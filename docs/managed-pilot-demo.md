# Managed Pilot Demo: Telegram Lead Triage

This is an example of the first Bux managed pilot workflow: qualify a Telegram inbound request and produce a concise human handoff.

The example is synthetic, but the workflow is the one used for the $1,000/month pilot.

## Scenario

A crypto infrastructure company gets partner and customer requests in Telegram. Today, humans ask the same questions repeatedly before they know whether sales, support, engineering, or compliance should respond.

The Bux operator sits in a private owner-controlled thread. It can draft replies, collect missing context, research public pages in the browser, and post a structured handoff.

## Transcript

Lead:

> We are launching a wallet integration next month and need help automating onboarding for partners. Can you support this?

Operator:

> I can help collect the right context before a human joins. What company or project are you from, and what kind of partners are you onboarding?

Lead:

> AcmePay. We are onboarding regional payment partners and need them to submit KYC docs, API details, and launch timelines.

Operator:

> Thanks. Which regions are involved, and roughly how many partners do you expect in the first month?

Lead:

> EU and LATAM first. Maybe 20 partners in the first month.

Operator:

> What system should the handoff land in today: Telegram only, CRM, ticketing, Sheets, or another tool?

Lead:

> Telegram first. We use HubSpot later, but Telegram is where the team starts.

Operator:

> Understood. Any legal, compliance, or production-access step that should always escalate to a human immediately?

Lead:

> Yes. Anything involving compliance approval or production API keys.

## Handoff

```text
Lead: AcmePay
Fit: high
Need: partner onboarding automation for wallet integration launching next month

Context collected:
- Regions: EU and LATAM
- Expected volume: about 20 partners in first month
- Partner inputs: KYC docs, API details, launch timelines
- Current workflow starts in Telegram; HubSpot may be used later

Missing:
- Internal owner for partner onboarding
- Exact KYC document checklist
- Desired HubSpot fields, if CRM handoff is added

Risk:
- Compliance approval and production API keys must always escalate to a human

Recommended next human action:
- Offer a 20-minute scoping call focused on one Telegram-first onboarding workflow
- Ask for 10-20 past partner onboarding threads and current KYC/API checklist
```

## What Bux did

- Asked one question at a time.
- Kept the lead in Telegram.
- Collected workflow facts before a human joined.
- Identified escalation boundaries.
- Produced a structured handoff the owner can act on.

## What Bux did not do

- Promise pricing, legal approval, compliance approval, discounts, launch dates, or production access.
- Pretend to be a human.
- Ask for secrets.
- Make an irreversible commitment.

## Pilot version

The live pilot adapts this pattern to one real workflow in 7 days.

- Pilot page: https://browser-use.github.io/bux/pilot.html
- Playbook: https://browser-use.github.io/bux/managed-pilot-playbook.html
- Apply on GitHub: https://github.com/browser-use/bux/issues/new?template=managed-pilot.yml
- Start on Telegram: https://t.me/Magnus_Mueller
