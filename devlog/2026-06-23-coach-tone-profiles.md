# 2026-06-23 — You can pick how the coach talks to you now

The brief had one voice: supportive when things were going well, and a roast when
I was slipping. That blend is mine. It is baked into the prompt, and it was the
only option. So I made the voice a profile you choose.

There are four. Supportive is your biggest believer; it frames every read as a
bounce-back and never tears you down. Neutral takes the emotion out and tells you
how it is against your goals. Hardass is cynical and never satisfied; anything
short of overachieving gets called out, and it always pushes for more. And
adaptive, the default, is the old behavior unchanged, so a fresh clone gets
exactly what it got before.

Each profile is a real file you can read and edit, with a written-out voice and a
set of numbers: harshness, warmth, push, and two thresholds for when the tone
hardens or celebrates. You pick one with a config command, and you can override
any single number the same way.

## Making the numbers actually mean something

The hard part, and the thing the review pushed me on, was whether numbers attached
to a tone do anything at all. A coach reads "rip you apart" from the prose and may
just ignore a "harshness: 9" sitting next to it. Numbers that can't be falsified
are decoration.

So the thresholds carry real, testable weight. For the parts of the brief with a
concrete goal, like steps and plan adherence, the profile deterministically decides
whether the harsh-tone instruction block goes into the prompt at all. A harsh
profile assembles it; a soft one omits it entirely, and the prose voice handles the
rest. That is a switch in code, not a hope about the model, and there is a test
that fails if it breaks. The zero-to-ten dials stay, framed honestly as calibration
hints rather than precise controls.

## Tested against expectations, not by reading

Every profile is checked against what it is supposed to do, automatically. A
deterministic scorer runs all four and asserts each one keeps the output schema,
the four tone categories, and the jargon translation, and that the harsh-block
switch is right for that profile. That is in the test suite, so it gates every
change. On top of that, the brief generates under each profile on demand, and the
default's cross-model run came back consistent. The point was never to eyeball a
sample and call it good; it was to make the tone a thing the suite can verify.
