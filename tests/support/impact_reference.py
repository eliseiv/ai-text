"""The TOTAL function ``impact()`` — the single source of truth of the classification.

It lives in the TEST tree on
purpose: in production the ``(op, reason) → impact`` table is a PROJECTION (R-OBS-7a — computing
``credited`` at runtime would mean a SELECT into ``ledger_transactions`` on every exit path, i.e.
loading the money path for the sake of a metric label). The projection is legitimate ONLY while a
test recomputes the function and compares — which is what this module exists for.

⚠️ NOTHING HERE MAY BE FED FROM A TABLE IN ``docs/``. The inputs are derived from a RUN of the real
code (verifier call counters, rows in ``ledger_transactions``, an EXECUTED remediation replay) —
see ``tests/integration/test_billing_impact_truth.py``. Feeding the inputs from the same document
that declares the answer would prove only that the document agrees with itself.
"""

from __future__ import annotations

from typing import Literal

Money = Literal["confirmed", "claimed", "absent"]
Remediation = Literal["credit_after_fix", "investigate"]

IMPACT_NONE = "none"
IMPACT_LOST_PAYMENT = "lost_payment"
IMPACT_REFUND_NEEDED = "refund_needed"
IMPACT_UPSTREAM = "upstream"

# The rows of the "Как вычисляется" table. The signature test asserts
# this set equals the parameter list of `reference_impact` — a parameter with no row (or a row with
# no parameter) is exactly the defect that let `credits` through with the wrong meaning.
DOC_INPUT_ROWS = {"money", "credited", "deliberate", "system_broken", "remediation"}


def reference_impact(
    *,
    money: Money,
    credited: bool,
    deliberate: bool,
    system_broken: bool,
    remediation: Remediation,
) -> str:
    """Total function — verbatim.

    ``credited`` is "was the PAYER left uncredited", not "did THIS branch grant something": on a
    re-delivery (the routine high-volume path) the grant already happened on an earlier event of
    the same period, and a naive ``credits == 0`` would fire the lost-payment alert on every retry
    of every webhook — burying the most important alert of the system under noise (R-OBS-4).
    """
    if money == "confirmed" and not credited:
        return IMPACT_REFUND_NEEDED if deliberate else IMPACT_LOST_PAYMENT
    if money == "claimed" and not credited:
        return IMPACT_LOST_PAYMENT if remediation == "credit_after_fix" else IMPACT_UPSTREAM
    if system_broken:
        return IMPACT_UPSTREAM
    return IMPACT_NONE
