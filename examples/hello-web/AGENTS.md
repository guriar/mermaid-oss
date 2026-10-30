# hello-web agent workflow

hello-web is one page and one API on the Python standard library. This file is what an agent
reads first. The harness is the mermaid plugin; its doors are `/hotfix` for one small change
with the owner present, `/sweep` for several at once, `/factory` for an ask of any size the
session works itself, `/docket` for what agents raised, and `/demo` for what shipped. Run `/hotfix "<the ask>" --shadow` to see a run that pushes
nothing and opens no PR.

## How this product is reached
The venue is `tools/venue.py`: it starts `app/server.py` on a free port, builds the three
states in `tools/venue-states.json`, and answers their checks through the API. It declares
`ui` and `states`; it has no accounts, no model, no deploy and no database, so a run's record
reads `not offered` for those.

## How we write code
- Standard library only; a dependency needs a reason written here and the owner's yes.
- One test pins one behaviour (`tests/`), run by `python -m unittest -q`, the one gate.
- A new state the page can be in enters `tools/venue-states.json` with its build, undo and
  check in the same change, because a state with no check is one nobody can ask about.
- No em dashes in copy.
