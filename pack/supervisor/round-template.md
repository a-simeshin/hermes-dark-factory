# Teaching round template

One round per new kind of mistake. Facts first, then the expected outcome, never the edit itself: the agent finds the root cause in its own session and decides which knowledge layer the lesson belongs to.

```
Round <N> (from the owner's supervisor, through the console). <One-line topic.>

Facts:
- <card id, PR or comment link, timestamp>: <what happened, quoted>.
- <the measured effect: duplicate card, lost step, tokens, idle hours>.
- <why it breaks the design: the rule or document it contradicts>.

What I expect (outcomes, not edits):
1. Now: <stop the damage on the live board without losing work>.
2. Systemically: <the class of mistake no longer happens>. First check whether a
   native Hermes mechanism already covers it, and extend an existing mechanism of
   ours before adding a new one.
3. <check the related places for the same mistake>.

Answer briefly: what changed and where (file:line), which test proves it (red on the
old behaviour), the knowledge snapshot hash, and a last line DONE.
```

Checklist before sending:

- the facts come from a live query made now, not from an old comment;
- the request does not contradict a recorded owner decision;
- the step you ask for has a moment where the acting agent actually has a turn;
- the round is single-topic.
