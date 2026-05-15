# Review Checklist

Before finalizing, check:

## Correctness
- Does the change solve the requested problem?
- Are edge cases handled?
- Are failures explicit?

## Scope
- Did we only touch files in the approved boundary?
- Did we avoid unrelated cleanup?
- Is the diff small enough?

## Coupling
- Did we avoid new circular imports?
- Did we avoid hidden global state?
- Is domain logic still separate from CLI/reporting?

## Tests
- Were relevant tests added or updated?
- Were tests run?
- If not, why?

## Final Answer
Report:
- summary
- changed files
- tests
- risks
- next step
