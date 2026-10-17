# Feedback for Nebius, NVIDIA and Tavily

I built mutagent alone over about two weeks: an agent that writes pytest tests with NVIDIA Nemotron models on Nebius Token Factory and keeps only tests that raise the mutation score. Everything below comes from that work. Each item says what I did, what happened, what I expected, and a concrete suggestion.

## Summary: the five things that would have helped most

1. **A hard spending cap that stops usage at $0** instead of charging the card. As a student I could only use the hackathon credits once I accepted "If it gets to $0.00, we'll charge your payment method daily".
2. **Documented control over Nemotron Super's reasoning budget** on Token Factory. Replies regularly came back empty because the whole `max_tokens` budget went into reasoning.
3. **Self-serve access to Token Factory Sandboxes** for hackathon projects. The Coding track asks for "agents that write, run, and test code in Token Factory", but Sandboxes needs a manual activation by email.
4. **Clearer credit onboarding**: the card was required before the promo code could even be entered, the trial and the promo balance behave differently, and the first "Switch to paid usage" click failed.
5. **A credit-covered way to host a small CPU web demo** on Nebius, since the rules require a testable demo until the end of judging.

## Nebius Token Factory

### What worked well
- **The OpenAI-compatible API worked unchanged** with the official `openai` Python client: only the base URL and the key changed. Moving from a local fake model to real Nemotron calls took minutes.
- **Prices are low enough to experiment freely.** A full recording of my demo (8 agent and baseline runs, about 780,000 tokens, mostly Nemotron 3 Super) cost roughly $0.30-0.50. Single runs used 8,000-190,000 tokens.
- **Both tiers in one place**: Nemotron 3 Super for the heavy work and Nemotron 3 Nano for cheap helper calls, behind the same key.
- I saw no rate-limit errors during development, including long sequential runs.

### Friction and suggestions
| What I did | What happened | What I expected | Suggestion |
|---|---|---|---|
| Called `nvidia/nemotron-3-super-120b-a12b` for test generation | Many replies came back with empty content and `finish_reason=length`: the reasoning used up the whole `max_tokens`. Sometimes the reasoning text ended up in `content` instead of a separate field. I had to raise `max_tokens` to 16,000 and add prompt rules ("keep reasoning short, reply with one code block") | A documented way to cap or disable reasoning per request, and reasoning always in its own field | Document for each reasoning model whether `chat_template_kwargs` (for example `enable_thinking`) or a reasoning-budget parameter is honoured, and return reasoning separately from `content` |
| Tried to switch thinking off | I passed `chat_template_kwargs: {"enable_thinking": false}` through `extra_body`, but could not confirm from the response whether it had any effect | A clear yes/no in the docs, or a response field showing the reasoning mode used | Echo the effective reasoning settings in the response, or list supported `extra_body` options per model in the catalog |
| Copied the code snippet from the model page | The snippets use a region-specific base URL, and it was not obvious which one applies to my project | One base URL, or the snippet filled in for my project | Generate the snippet with the project's own endpoint, and say on the page which regions exist |
| Looked up model details | IDs, context length and prices are spread over the catalog, the model page and the pricing page | One table per model with ID, context, price in and out, reasoning yes/no | A comparison table in the catalog |

## Billing and credits (the part that cost me the most time)

| Date | What I did | What happened | Suggestion |
|---|---|---|---|
| 2026-10-14 | Wanted to redeem the hackathon promo code | The billing-details form with a card was required on login, before I could even reach the promo-code field | Let people redeem a hackathon code first and ask for a card only when they want paid usage |
| 2026-10-14 | Chose "stop usage when the trial ends" to avoid any charge | Sensible, but it was not clear that the trial and the $29.75 promo balance are separate pots | Show "trial: X left, promo credits: Y left" separately, and say which one is used first |
| 2026-10-17 | My scripts suddenly failed | "Trial ended. Usage stopped." The promo balance was untouched but unusable until I switched to paid usage | Use promo credits automatically after the trial, or at least say "you still have $29.75 in credits: click here to use them" |
| 2026-10-17 | Clicked "Switch to paid usage" | First attempt: "Payment failed. Top up your balance" (with a request ID). A second attempt a few minutes later worked | Explain what the payment check does, and why it failed, in the error message. Suggesting "Top up" here is risky for users who only want to spend credits |
| 2026-10-17 | Read the banner after switching | "If it gets to $0.00, we'll charge your payment method daily." There is no option to stop at $0 instead | Offer a hard cap or a "stop at $0" setting for paid usage, plus email alerts at thresholds. This is the single change that would make students comfortable building on Nebius |

## Token Factory Sandboxes

| What I did | What happened | Suggestion |
|---|---|---|
| Read the hackathon page | The Coding and Agentic Engineering track describes "agents that write, run, and test code in Token Factory", but the hackathon Resources page does not mention Sandboxes at all | Link the Sandboxes docs from the track description and the Resources page |
| Tried to get access | Sandboxes is in beta. The access page says a project may show `inactive` and to email contree@nebius.com with the project ID; there is no self-serve switch | Activate Sandboxes automatically for hackathon participants, or add a request button in the console that shows the request status |
| Read the Python SDK reference to plan the integration | Files can be uploaded (`upload`, `upload_bytes_file`), but I could not find how an uploaded file is attached to a run or an image, and the quickstart has no upload example | Add a recipe "upload a project folder and run pytest in it", which is exactly what coding agents need |

## NVIDIA Nemotron models

### Nemotron 3 Super (writing tests)
- **Good at the actual task.** Given the source and the surviving mutants as diffs, it regularly wrote tests that caught most of them. It took a toy module from 37.8% to 97.3% and cachetools `_cached.py` from 23.8% to 96.2%.
- **Common failure: wrong expected values.** It often guessed what a function returns instead of reading it (for example `'Ss'` vs `'ß'`, `info.miss` vs `info.misses`, `IOError` vs `IoError`). Giving it the real pytest output of its failed tests fixed most of these in one repair call. That repair step became the single biggest improvement in my agent.
- **It tried to "call the mutant" inside a test** (asserting what the mutated code would return) until the prompt said explicitly that tests only ever run against the real code.
- **Output-format slips** I had to handle: code blocks that were opened but never closed, replies with no code block at all, and long think-aloud comments ("Wait... Actually...") inside the test file.
- **Mutating shared state**: some tests cleared module-level registries (`PLURALS.clear()`), which broke the library's own tests. A prompt rule fixed it.

### Nemotron 3 Nano (helper calls)
- **Good** at one-sentence explanations of why a bug survived. Cheap and fast enough to run once per run.
- **Not reliable for judging equivalent mutants.** Asked which surviving mutants are unkillable, it once flagged 23 of 23 (most were easy to catch) and another time flagged a mutant that the agent then caught. I switched that triage off by default.

### Suggestions for NVIDIA
- Publish prompt guidance for code generation with Nemotron 3 Super: how to get exactly one fenced block, and how to keep reasoning out of code comments.
- State in the model card how reasoning is controlled when the model is served through OpenAI-compatible APIs.

## Tavily

- **Worked on the first try**: plain HTTPS `POST /search` with a Bearer key, clear docs, useful `include_answer`.
- **For real libraries the results were exactly right**: lookups for cachetools functions returned `cachetools.readthedocs.io` pages.
- **For generic function names the results were off-topic** (a toy `apply_discount` returned Stripe and freeCodeCamp pages). I now drop results that don't mention the package. A "documentation of package X" mode, or a documented `include_domains` recipe for Read the Docs, PyPI and GitHub, would make this easier for coding agents.

## Hosting the demo

| Date | What I did | What went wrong | Suggestion |
|---|---|---|---|
| 2026-10-14 | Looked at Nebius Serverless Endpoints to host a small CPU-only web demo | The endpoint docs describe deploying a container image, but I could not find whether CPU-only presets exist, whether hackathon credits cover them, or whether an endpoint can scale to zero when idle | Add a "host a small web app" page with an example, preset list and price per hour, and say how idle endpoints are billed |
| 2026-10-14 | Compared hosts for the live demo link | The rules require a testable demo until the end of judging, but the Resources page does not say which hosting options the organisers expect | Add a hosting section to the Resources page, ideally with a credit-covered Nebius option. I used Render's free tier; only the model calls go to Nebius |

## Hackathon onboarding

| Date | What I did | What went wrong | Suggestion |
|---|---|---|---|
| 2026-10-13 | Applied to the Nebius AI Builder Program | Expected to claim credits right after verifying my email; instead got a "we are reviewing your application" email promising an update within two business days | State the review step and timeline on the Builders page and the hackathon Resources page |
| 2026-10-13 | Submitted the hackathon promo-code form | The confirmation page only says the code "will be emailed to you", with no time estimate | Say how long delivery takes, and where to redeem the code afterwards |
| 2026-10-13 | Looked for the Discord invite | The hackathon Resources page and the Builders page link to two different Discord invites | One canonical link, or say which channel is for hackathon questions |
