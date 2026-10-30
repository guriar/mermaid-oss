<!-- hotfix issue: filed from docket {{id}} by the walk. One issue per CAUSE. The /hotfix
     lane starts at the branch because this body already did the investigation.
     Labels at creation: hotfix + the territory (<a territory of the product's tree>). -->

## What the owner sees
{{plain}}

## Where
- Venue and SHA: <the tier the product names, local or deployed> at <sha>
- Route or surface: <path>
- Viewport: <the viewports the product names>
- Venue: <the venue name>

## Reproduce
1. <step>
2. <step>
3. <what renders or returns, quoted>

## Cause ({{cause_class}})
{{cause}}

## Desired
{{desired}}

## Recurrence
{{recurrence}}

## Fence
- Files expected to change: <list>
- Eligibility, pre-answered: one territory <yes/no> · no migration <yes/no> · no new dependency <yes/no> · no request field added or removed <yes/no> · no auth or guarded path <yes/no> · no mock needed <yes/no>
- Anything "no" above means this is a slice, not a hotfix; retype it.

## Done when
- <assertion: expected | how measured> (the lane copies these into before/ and after/ assertions.md)
- <assertion>
- One pin test recreating the observed failure, citing this issue.

## Evidence on file
{{evidence}}

---
Docket: {{id}} · discovered by {{discovered_by}} · wave {{wave}} · filed {{ts}}
