"""Business logic for the audit domain.

Services own authorization (ctx.require), data scope, business rules, transactions and audit.
Routers, the AI layer and workers all call these functions.
"""
