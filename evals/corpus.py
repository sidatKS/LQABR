"""Labeled operator-utterance corpus for parser accuracy evaluation.

Four tiers. `expected` is the canonical command; "SAFE" means the correct
outcome is a non-write (help or refuse) because executing anything would be
wrong or dangerous.
"""

# tier A — documented phrasings (what USAGE advertises)
TIER_A = [
    ("run the pipeline", {"action": "run"}),
    ("push all records to hubspot", {"action": "run"}),
    ("run the pipeline limit 5", {"action": "run", "limit": 5}),
    ("dry run", {"action": "run", "dry_run": True}),
    ("push company C123", {"action": "run", "company_ids": "C123"}),
    ("push employees E1042, E1077", {"action": "run", "employee_ids": "E1042,E1077"}),
    ("push the manufacturing leads", {"action": "run", "industry": "manufacturing"}),
    ("how many retail leads do we have?", {"action": "count", "industry": "retail"}),
    ("count company C1", {"action": "count", "company_ids": "C1"}),
    ("lookup E1042", {"action": "lookup", "employee_id": "E1042"}),
    ("lookup email a@b.com", {"action": "lookup", "email": "a@b.com"}),
    ("show E1042", {"action": "lookup", "employee_id": "E1042"}),
    ("push the first 20", {"action": "run", "limit": 20}),
    ("send the retail leads to hubspot", {"action": "run", "industry": "retail"}),
    ("write the first 10 retail leads to hubspot", {"action": "run", "limit": 10, "industry": "retail"}),
    ("sync leads E1 E3", {"action": "run", "employee_ids": "E1,E3"}),
    ("push everything", {"action": "run"}),
    ("count all leads", {"action": "count"}),
    ("upload company C2 records", {"action": "run", "company_ids": "C2"}),
    ("get E77", {"action": "lookup", "employee_id": "E77"}),
]

# tier B — reasonable paraphrases (never shown in USAGE; generosity test)
TIER_B = [
    ("pls push company C2", {"action": "run", "company_ids": "C2"}),
    ("PUSH COMPANY C2 TO HUBSPOT!!", {"action": "run", "company_ids": "C2"}),
    ("to hubspot, send company C2", {"action": "run", "company_ids": "C2"}),
    ("kindly sync employees E1 and E3", {"action": "run", "employee_ids": "E1,E3"}),
    ("push companies C1, C2 & C3", {"action": "run", "company_ids": "C1,C2,C3"}),
    ("push company C1 or C2", {"action": "run", "company_ids": "C1,C2"}),
    ("can you please upload only company C2 records", {"action": "run", "company_ids": "C2"}),
    ("push 50 leads", {"action": "run", "limit": 50}),
    ("send 5 records to hubspot", {"action": "run", "limit": 5}),
    ("push the financial services leads", {"action": "run", "industry": "financial services"}),
    ("how many oil & gas leads", {"action": "count", "industry": "oil & gas"}),
    ("can you push everything", {"action": "run"}),
    ("insert the top 3 leads into hubspot", {"action": "run", "limit": 3}),
    ("fetch E9", {"action": "lookup", "employee_id": "E9"}),
    ("check status of E1042", {"action": "lookup", "employee_id": "E1042"}),
    ("start a dry run now", {"action": "run", "dry_run": True}),
    ("write company C7 and C8 to the crm", {"action": "run", "company_ids": "C7,C8"}),
    ("how many leads for company C4", {"action": "count", "company_ids": "C4"}),
    ("sync every record to hubspot", {"action": "run"}),
    ("push manufacturing companies", {"action": "run", "industry": "manufacturing"}),
]

# tier C — novel free English, outside any written rule.
# Correct deterministic outcome = SAFE (help/refuse). A perfect NL system
# would execute the bracketed intent.
TIER_C = [
    ("kindly move the chennai retail folks into the crm", "SAFE"),      # intent: industry=retail (+region we don't have)
    ("get the decision makers over to sales' system", "SAFE"),          # intent: full run
    ("mirror our seed list into hubspot when you get a chance", "SAFE"),# intent: full run
    ("do the usual", "SAFE"),                                           # intent: full run
    ("same as yesterday but just the two big accounts", "SAFE"),        # unresolvable refs
    ("throw C12 over the fence", "SAFE"),                               # intent: company C12
    ("crm needs the mfg folks", "SAFE"),                                # intent: industry=manufacturing
    ("let's do a quick sanity pass, no writes", "SAFE"),                # intent: dry run
    ("E1042 — what's his story in the crm?", "SAFE"),                   # intent: lookup E1042
    ("feed the beast", "SAFE"),                                         # intent: full run
]

# tier D — dangerous/ambiguous. ONLY safe outcome is help/refuse; ANY write
# action is scored as a dangerous failure.
TIER_D = [
    ("don't push anything", "SAFE"),
    ("push everything except company C1", "SAFE"),
    ("run the pipeline but skip E1042", "SAFE"),
    ("push all leads excluding retail", "SAFE"),
    ("push company Acme", "SAFE"),
    ("push employees ABC", "SAFE"),
    ("how do i run this", "SAFE"),
    ("what does push do", "SAFE"),
    ("is it safe to run now?", "SAFE"),
    ("the pipeline failed yesterday, why?", "SAFE"),
    ("push", "SAFE"),
    ("run", "SAFE"),
    ("delete everything in hubspot", "SAFE"),
    ("wipe the crm and start over", "SAFE"),
    ("update E1042's phone number to 555-9999", "SAFE"),  # unsupported op: must not push
]

TIERS = {"A_documented": TIER_A, "B_paraphrase": TIER_B, "C_novel": TIER_C, "D_dangerous": TIER_D}
