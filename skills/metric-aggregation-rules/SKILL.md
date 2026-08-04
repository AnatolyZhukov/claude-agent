---
name: metric-aggregation-rules
description: Rules for correctly combining and explaining additive vs non-additive metrics when reporting on sample_superstore data (revenue, orders, discounts vs active users, averages, ratios).
---

# Metric Aggregation Rules

Before combining, summing, or comparing any metric across time periods,
regions, or categories, classify it first.

## Additive metrics

Metrics where each row/event contributes independently, so partial results
can be safely summed.

Examples: revenue, profit, quantity sold, number of orders, discount amount.

Rule: `sum(sub-periods) == value(full period)`. It is safe to add together
partial results returned by separate tool calls (e.g. revenue per region).

## Non-additive metrics

Metrics that CANNOT be summed across sub-periods or sub-groups without
producing a wrong answer, because they depend on distinct values, ratios, or
statistical aggregates.

Examples: distinct/active users (`COUNT DISTINCT`), average order value,
conversion rate, churn rate, percentages, medians.

Rule: never sum partial results for these. Recompute over the full combined
range/group in a single query. If the available tools can only return
partial figures, say so explicitly instead of approximating by summation.

## Procedure

1. List every metric the user is asking for.
2. Classify each one as additive or non-additive using the rules above.
3. For additive metrics, combine values from multiple tool calls by summing
   them.
4. For non-additive metrics, issue one tool call covering the full requested
   range/group. Do not sum partial calls. If that's not possible with the
   available tools, tell the user the number would be inaccurate and explain
   why, rather than guessing.
5. When a response mixes both kinds of metrics, label which numbers were
   summed and which were computed directly, so the user knows how each
   figure was derived.
