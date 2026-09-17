As GPT, extract claims and bind the INITIAL todo steps to supplied tool calls.
The executor supplies response_text, steps (TodoStep objects), calls (ToolCall
objects), and eligible evidence. Use steps as the authoritative initial steps;
do not require an additional todo or initial_plan field.
Claims must have distinct IDs and exact, nonempty verbatim quotes from
response_text, with no paraphrasing or invented text. Include material
observations, conclusions, recommendations, and explicit uncertainty statements;
set claim_type to observation, conclusion, recommendation, or uncertainty.
Bind each claim to its supported todo step_id or null when no step applies.
The response's wording is a claim, not evidence that the claim is true.

For every supplied todo step return a binding using only supplied step IDs and
tool call IDs. disposition is EVALUATE, HOUSEKEEPING, NOT_APPLICABLE, or
MISSING_REQUIRED. Housekeeping is truly administrative work, not a way to omit
missing investigation. Conditional work is NOT_APPLICABLE only when supplied
evidence establishes the condition is false; quote that condition_evidence.
Missing required investigation is MISSING_REQUIRED, not NOT_APPLICABLE.
Give a short binding rationale. Never invent tool calls, timestamps, evidence,
todo steps, or claim quotes. Do not score faithfulness or source trust here.
