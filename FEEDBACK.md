# Feedback for Nebius and NVIDIA

Keep this up to date while building. The hackathon asks for feedback on Token Factory, AI Cloud and any NVIDIA tools used, and the Most Valuable Feedback prize is judged on completeness, viability and potential impact, so be specific.

For each item: what you tried, what happened, what you expected, and a concrete suggestion.

## Nebius Token Factory

- Setup and first call (time to first successful request, doc clarity):
- Model catalog (finding model IDs, context limits, pricing visibility):
- Latency and throughput observed for each tier:
- Errors and rate limits encountered:
- What worked really well:

## Nebius AI Cloud (Serverless Endpoints / Jobs / DevPods)

- Deployment experience:
- Pain points:

## NVIDIA Nemotron and other NVIDIA tools

- Quality of generated tests per model:
- Reasoning-block handling (`<think>` output) and instruction following:
- Anything surprising:

## Friction log

| Date | What I did | What went wrong | Suggestion |
|---|---|---|---|
| 2026-10-13 | Applied to the Nebius AI Builder Program | Expected to claim credits right after verifying my email; instead got a "we are reviewing your application" email promising an update within two business days | State the review step and timeline on the Builders page and the hackathon Resources page |
| 2026-10-13 | Submitted the hackathon promo-code form | Confirmation page only says the code "will be emailed to you"; no time estimate | Say how long delivery takes, and where to redeem the code afterwards |
| 2026-10-13 | Looked for the Discord invite | The hackathon Resources page and the Builders page link to two different Discord invites | One canonical link, or say which channel is for hackathon questions |
| 2026-10-14 | Looked at Nebius Serverless Endpoints to host a small CPU-only web demo | The endpoint docs describe deploying a container image, but I could not find whether CPU-only presets exist, whether hackathon credits cover them, or whether an endpoint can scale to zero when idle | Add a "host a small web app" page with an example, preset list and price per hour, and say how idle endpoints are billed |
| 2026-10-14 | Compared hosts for the live demo link | The hackathon rules require a testable demo until the end of judging, but nothing on the Resources page says which hosting options (free or credit-covered) the organisers expect | Add a short hosting section to the Resources page, ideally with a credit-covered Nebius option |

## Notes to fill in once credits arrive

- Token Factory base URL and model IDs: where did I find them, and how long did it take?
- Real latency per tier, tokens per run, cost per run.
- Does the `openai` Python client work unchanged against Token Factory? Any differences (reasoning blocks, usage fields, errors)?
- Which Nemotron model wrote better tests for surviving mutants, and where did each one fail?
