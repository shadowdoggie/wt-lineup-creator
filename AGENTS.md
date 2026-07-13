# Working with Dylan

Dylan is not a programmer and has ADHD — long text gets skipped entirely. These rules exist so he never has to read a wall of text to stay in control of his own project.

## Every reply that changes something

End with exactly this block, max 3 short lines, plain English, zero jargon:

- **Changed:** what he'd notice when using the app/site.
- **Check:** the ONE thing to look at to confirm it works.
- **Unsure:** anything you're guessing at or can't verify (write "nothing" if none).

Longer detail may go *below* this block — never instead of it. If he must read something, it must be short.

## Before building a feature

- If correctness depends on real-world or domain knowledge (game data, external facts, "best of" rankings), FIRST state plainly whether your data source can actually support it. If it can't be done accurately, say so BEFORE writing any code and ask whether he wants a labeled approximation.
- Ask Dylan for 2–3 examples of known-correct output first (e.g. "at BR 6.7 the answer should be the Tiger II"). Treat them as acceptance tests: do not present results until they pass.

## When something is wrong

- If Dylan reports the same feature wrong TWICE, stop patching. Step back and tell him honestly whether it's fixable in principle or fundamentally limited by the data.
- Never present estimates, heuristics, or invented scores as facts. Label them as estimates.
- Judge results the way Dylan would — "does this output look right to someone who knows the domain?" — not by whether your own math is internally consistent.

## Always

- Commit and push after each coherent change (his repos are private).
- Be bluntly honest about uncertainty and about his requests. He explicitly prefers hard truth over polish.
