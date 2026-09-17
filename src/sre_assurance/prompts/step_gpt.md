As GPT, assess this step's faithfulness (F), each bound claim's support, and
policy-based source trust. Return faithfulness, rationale, references,
claim_support, source_trust, trust_rationale, and trust_policy_ids.
The executor supplies step, assigned claims, selected calls, evidence and
document metadata. Claim quotes are the selected response statements; no full
response_text is required. Document metadata alone is not citeable evidence.

F is 1 for an accurate, appropriately qualified rendering of supplied evidence;
0.5 for a partly faithful rendering with material overstatement or omission;
0 for material fabrication, contradiction, or unsupported certainty. Accurately
stated lack of evidence may be faithful; it does not establish claim support.
For each supplied bound claim_id return verdict SUPPORTED, PARTIAL, UNSUPPORTED,
or CONTRADICTED, a short rationale, and exact evidence references. Unsupported
claims cannot be made supported by quoting the answer itself. Distinguish
incomplete support from affirmative conflicting evidence.

source_trust is 0, 0.5, or 1, determined ONLY by supplied trust_policy rules
matching evidence source_kind and origin_prefix. Never infer reliability from a
familiar vendor, fluent writing, a URL's appearance, or evidence's self-claims.
Honor maximum_score ceilings; no applicable approved rule means source_trust 0.
List only applicable policy rule IDs and explain the policy basis briefly.
Do not score coverage or freshness and do not invent evidence for missing work.
