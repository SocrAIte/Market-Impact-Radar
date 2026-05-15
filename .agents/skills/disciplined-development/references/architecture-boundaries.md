# Architecture Boundaries

## Desired Layers

Prefer this shape:

1. Input layer
   - reads JSON/CSV/API data
   - validates schemas
   - does not contain business scoring logic

2. Domain layer
   - signal detection
   - mapping rules
   - scoring
   - filtering
   - pure functions where possible

3. Report layer
   - markdown/html/csv output
   - presentation only
   - no hidden scoring logic

4. CLI layer
   - argument parsing
   - command dispatch
   - no business rules

## Forbidden Coupling

Avoid:
- CLI importing implementation details from many modules
- report generation recalculating scores
- data loaders knowing about ETF selection logic
- scoring functions reading files directly
- mapping tables scattered across unrelated files
