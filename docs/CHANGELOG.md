# Changes

## 0.6.2

- Sign-in immediately opens a dedicated account window, using the same official login flow as Workspace. Remote content runs in an isolated account session. Successful authorization returns to Control; temporary failures show a retry page. Pending login survives a page reload, network errors retry within the flow lifetime, and expired official cookies return to sign-in.
- Project registration and enrollment preserve current memory metadata and report document readiness.
- Bounded agent lease and finish commands support explicit working intervals and independent release verification.
- Finish tolerates a lost disconnect response, reports unconfirmed release as failure and detects new memory revisions even when their content hash is unchanged.
- Current UI no longer includes unreachable historical canvas and management implementations. Source formatting, module boundaries, development checks and isolated public CI are standardized.
- Expired explicit communication leases cannot present cached execution as current. Late account reads cannot undo logout; malformed account responses preserve the last verified state with an error.
- Invalid feedback classification types return a controlled input error.
- The OS protocol adapter is included in runtime packages instead of depending on an excluded historical directory. Existing registered policies and ledgers remain intact.

Release activation and platform availability are determined by the signed server manifest. macOS packages remain ad hoc signed, without Apple notarization.

## 0.6.1

Private leader feedback, persistent retries, account isolation and maintenance receipts; authenticated distribution and origin access protections.
