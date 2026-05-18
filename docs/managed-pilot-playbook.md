# Bux Managed Pilot Playbook

This is the operating plan for the $1,000/month managed Bux pilot.

The pilot is for teams that already use Telegram for sales, support, partner onboarding, or operations. It launches one private AI operator workflow in 7 days, with human handoff and weekly tuning.

## Best-fit workflow

The first workflow should be narrow and valuable enough that a faster handoff matters.

Good fits:

- Qualify inbound Telegram leads before sales joins.
- Collect missing onboarding details from partners or customers.
- Summarize support requests into a clean handoff.
- Check browser-only or logged-in pages and report status.
- Follow up later when a lead, ticket, CI run, launch, or external event changes.

Poor fits:

- Fully autonomous sales promises.
- Regulated advice or final approval decisions.
- High-volume consumer support with no human owner.
- Anything that requires hiding AI usage from users or staff.

## Pilot deliverables

The first 7 days produce:

- Private Bux runtime for the team.
- Telegram control path and owner handoff.
- Workflow prompt, rules, and escalation criteria.
- Intake fields for the chosen workflow.
- Handoff summary format.
- Scheduled check behavior where useful.
- Weekly tuning plan.

The first month should prove:

- Faster first response or fewer dropped leads.
- Less repetitive human triage.
- Cleaner context before a human decides.
- Clear enough value to continue at $1,000/month.

## Example workflow: inbound Telegram lead triage

Trigger:

- A new inbound request arrives in a Telegram group, topic, bot, or forwarded thread.

Operator goal:

- Understand who the person is, what they need, whether they fit the business, what is missing, and who should respond.

Questions the operator can ask:

- What company or project are you from?
- What are you trying to launch or integrate?
- What timeline are you working against?
- What monthly volume, user count, or operational scale matters?
- Which region or legal entity is involved?
- Who should join from your side?

Escalation criteria:

- Pricing, legal, compliance, payments, or contract commitments.
- Angry or sensitive support requests.
- Anything involving production credentials, funds, or irreversible actions.
- High-intent leads that are ready for a human call.

Handoff summary:

```text
Lead: <name / handle / company>
Fit: high | medium | low | unclear
Need: <one-sentence request>
Context collected:
- <fact>
- <fact>
- <fact>
Missing:
- <missing detail>
Risk:
- <legal/compliance/payment/security note, if any>
Recommended next human action:
- <reply, call, intro, reject, or ask one specific question>
```

## Example operator rules

```text
You are the first-line operator for a Telegram-heavy business workflow.

Your job is to collect context, reduce repetitive human triage, and produce clear handoffs.

Do:
- Ask one concise question at a time.
- Keep the tone professional and direct.
- Summarize facts, not guesses.
- Escalate when pricing, legal, security, money, production access, or customer frustration appears.
- Record what is missing before handing off.

Do not:
- Promise pricing, discounts, legal outcomes, compliance approval, shipping dates, or refunds.
- Request secrets or private keys.
- Pretend to be a human.
- Take irreversible action without the human owner.
```

## Demo script

1. A lead asks in Telegram: "Can your team help us automate partner onboarding?"
2. The operator asks for company, region, workflow, volume, and timeline.
3. The operator researches the company website in the browser if available.
4. The operator posts a handoff summary to the owner.
5. The owner replies with the final human response or asks the operator to schedule a follow-up.

## Pricing

Managed pilot:

- $1,000/month.
- One scoped workflow.
- Launched in 7 days.
- Weekly tuning during the pilot.

Optional expansion after the pilot:

- More workflows.
- More integrations.
- Tighter CRM, ticketing, or data warehouse handoff.
- Dedicated monitoring and response-time targets.

## Start

Start on Telegram: https://t.me/Magnus_Mueller

Apply on GitHub: https://github.com/browser-use/bux/issues/new?template=managed-pilot.yml

Pilot page: https://browser-use.github.io/bux/pilot.html
