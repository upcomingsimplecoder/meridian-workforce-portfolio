# Deterministic money and calendar costing

Status: implemented and verified locally with independent Fraction oracles.

Authoritative money, rates, and FTE never use binary floating point. The engine
uses Decimal with a fixed local precision and explicit currency exponents.
Exact quotient/remainder arithmetic applies half-even rounding without first
approximating repeating fractions.

Workforce cost follows a monthly convention: annual salary divided by twelve,
multiplied by active calendar days divided by days in that month, then FTE.
Business dates use America/New_York; recorded events use UTC. Benefits, employer
tax, and variable accrual are explicit synthetic policy assumptions. They are
not statutory payroll calculations.

Each person/month/component converts through a sealed directional FX set and
rounds to reporting minor units. Totals sum those integers. A transfer month
allocates the rounded amount to departments by largest remainder, with a stable
tie-breaker. Separate permitted department reads therefore reconcile with the
company total.

Scenario revisions pin source, policy, FX, operation rules, and calculator
versions. Missing coverage, unsupported currency, and corrupt pins fail closed.
Equivalent history splitting must produce the same cost. The leap-day hire
example adds exactly USD 140,482.76 under these rules.

Compensation has separate semantics: annualized merit reserves budget, first-year
merit reports monthly impact, and a prorated bonus pays once. Reporting FX
converts each rounded person reservation before aggregation. Re-converting a
pooled original-currency total would introduce drift and is not permitted.
