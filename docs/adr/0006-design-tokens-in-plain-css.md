# 6. Plain CSS custom properties and CSS Modules, and an ink scale for evidence

Date: 2026-09-11
Status: Accepted

## Context

The interface is a dense professional tool: tables of applications, a board of
stages, and a fit breakdown that repeats one small judgement — covered, partial, or
missing — dozens of times per posting. It is read for an hour at a time, not scanned
for ten seconds.

Two decisions follow from that, and both are easier to get wrong than to get right.

The first is how styles are written. A utility framework is the default, and for a
marketing site it is the right default. For this, the styling surface is small and
highly repeated, and the risk is not "slow to write CSS" but "three slightly
different greys for the same meaning".

The second is how the three-state evidence scale is coloured. Traffic lights are the
obvious answer and a bad one. Around 8% of men have a red-green colour vision
deficiency, which makes covered and missing indistinguishable for a meaningful share
of users on the single most important control in the product. Red also carries a
meaning it should not carry here: a missing requirement is normal, it is the reason
the tool exists, and painting it the same colour as a validation failure makes an
ordinary gap read as an error.

## Decision

**Styling.** One token file, `apps/web/src/styles/tokens.css`, holds every colour,
size, weight, radius, duration and layout constant. Components use CSS Modules and
resolve every value through a token; no component writes a literal colour or a
literal pixel size. `base.css` sets element defaults so a plain `<button>` or `<p>` is
already correct. No utility framework.

Two typefaces, self-hosted through `next/font` so no request leaves the origin and
there is no layout shift. IBM Plex Sans is the working face — it holds up at 12-14px
in dense tables and its numerals are genuinely tabular, which matters in a tool whose
job is comparing counts and dates down a column. Spectral is reserved for the things
that are documents: page titles, the fit-score numeral, and the rendered resume and
cover-letter previews. That is the whole rule, and it means the serif always signals
"this is a document" rather than "this is a heading".

**The evidence scale is ink density, not hue.** One accent, a deep petrol, used at
three densities:

| State | Fill | Border |
|---|---|---|
| Covered | solid accent | solid accent |
| Partial | accent wash | solid accent |
| Missing | none | dashed |

Each state differs from the others by fill *and* by border style, so it reads
correctly in greyscale, on a monochrome printout, and with any colour vision
deficiency. Red is kept for what red should mean: a bullet that failed the grounding
validator, a destructive action, an error.

**Contrast is enforced, not claimed.** `apps/web/scripts/check-contrast.mjs` parses
the token file, resolves both themes, and fails the build if any of the 22 documented
pairings drops below WCAG AA — 4.5:1 for text, 3:1 for component boundaries. It runs
as part of `npm run check`, which runs as part of `make check`.

## Alternatives

**Tailwind CSS v4.** Its `@theme` block is genuinely one token file, so the "tokens
in one place" requirement is satisfied either way, and it would be faster to write
the first few screens. Rejected because the repeated markup here is small and
component-shaped rather than layout-shaped, because utility classes in JSX make the
evidence scale's three states harder to read as a set, and because a build-time
framework is one more thing between the token file and the rendered pixel when
debugging a contrast failure. This is a close call, and it would go the other way on
a larger or less repetitive interface.

**Traffic-light colours for the evidence scale.** Instantly legible to most people
and needs no explanation. Rejected for the accessibility and semantic reasons above.

**A third typeface for numerals.** A monospace face is the usual way to get columns
of figures to line up. Unnecessary: `font-variant-numeric: tabular-nums` on Plex Sans
does it, and a mono face used for small labels is one of the more recognisable
template tells.

## Consequences

Every new colour has to be added to the token file *and* to the pairing list in the
contrast checker, or it ships unchecked. That is deliberate friction.

The contrast checker earned its place immediately: it failed on its first run because
`--rule-strong`, at 1.6:1, was being used both as a table divider and as an input
border. A divider is decorative and may be quiet; an input border is the only thing
telling you the control exists and must reach 3:1. The result is two tokens, `--rule`
and `--edge`, with the distinction written down rather than rediscovered.

Writing CSS Modules by hand is slower per screen than utilities. The bet is that a
small, highly repeated interface pays that back in consistency, and that bet should be
re-examined if the number of distinct screens grows well past a dozen.
