"""Tests for the grounding check.

The cases below are the real failures this was written for — each one is an
answer the agent actually produced, paired with the tool output it actually
had — alongside the correct answers that must not be flagged. Pure functions,
so the whole policy is exercised without an API call.
"""
import pytest

from grounding import (
    tool_numbers,
    ungrounded_claims,
    ungrounded_entities,
    ungrounded_numbers,
)

VOCABULARY = {"Sean Miller", "Tamara Chand", "Raymond Buch", "Ken Lonsdale",
              "Canon imageCLASS 2200 Advanced Copier"}


class TestToolNumbers:
    def test_reads_every_number_in_the_output(self):
        assert tool_numbers("Consumer: 267332.5653\nCorporate: -129108.25") == [
            267332.5653, -129108.25,
        ]

    def test_text_without_numbers_is_empty(self):
        assert tool_numbers("No rows returned.") == []


class TestUngroundedNumbers:
    def test_a_share_worked_out_in_prose_is_flagged(self):
        # The observed failure: the three totals were quoted correctly from the
        # tool, then turned into shares that were wrong (the real split is
        # 56.5/27.3/16.2).
        answer = "Consumer is roughly 63%, Corporate 30% and Home Office 7%."
        tools = "Consumer: 267332.5653\nCorporate: 129108.2509\nHome Office: 76552.2148"
        assert ungrounded_numbers(answer, tools) == ["63", "30", "7"]

    def test_a_total_summed_from_rounded_values_is_flagged(self):
        answer = "Q4 was $182,912.25. Full year 2024: $472,992.98"
        tools = "2024-Q4: 182912.2542\n2024-Q3: 130449.3752"
        assert ungrounded_numbers(answer, tools) == ["472,992.98"]

    @pytest.mark.parametrize("written, returned", [
        ("$8,981.32", "8981.3239"),   # rounded to cents
        ("$8,981", "8981.3239"),      # rounded to whole units
        ("47.1%", "0.4714"),          # ratio reported as a percentage
        ("3.9 days", "3.977959"),     # truncated rather than rounded
        ("-$1,983.43", "-1983.4285"),
    ])
    def test_a_figure_read_off_a_tool_result_is_accepted(self, written, returned):
        assert ungrounded_numbers(f"The value is {written}.", returned) == []

    def test_a_sign_written_as_a_word_is_accepted(self):
        # "losing $1,983" against a returned -1983.43: magnitudes are compared,
        # because prose routinely carries the sign in the wording.
        assert ungrounded_numbers("losing $1,983 overall", "-1983.4285") == []

    @pytest.mark.parametrize("answer", [
        "The top 5 sub-categories, in 3 categories",   # list/rank sizes
        "1. Chairs 2. Phones 3. Machines",             # numbering
        "across 4 orders",                             # small counts
        "revenue in 2023 versus 2024",                 # years
    ])
    def test_structure_and_years_are_not_treated_as_claims(self, answer):
        assert ungrounded_numbers(answer, "Chairs: 72837.2494") == []

    def test_a_small_number_with_a_unit_is_still_a_claim(self):
        # "7%" is the shape of the share-computed-in-prose error, so the
        # small-integer exemption must not swallow it.
        assert ungrounded_numbers("margin of 7%", "0.162") == ["7"]
        assert ungrounded_numbers("$7 per order", "12.5") == ["7"]

    def test_nothing_returned_means_nothing_is_supported(self):
        assert ungrounded_numbers("Revenue was $1,234.00", "No rows returned.") == ["1,234.00"]


class TestUngroundedEntities:
    def test_a_customer_no_tool_returned_is_flagged(self):
        # The observed failure: the tool returned the top 10 by profit, which
        # cannot contain a loss-making customer, yet the answer named one.
        answer = ("The most profitable customer is Tamara Chand, unlike "
                  "high-volume customers like Sean Miller who operate at a loss.")
        tools = "Tamara Chand, 8981.3239\nRaymond Buch, 6939.1797"
        assert ungrounded_entities(answer, tools, VOCABULARY) == ["Sean Miller"]

    def test_entities_present_in_the_tool_result_are_accepted(self):
        answer = "Sean Miller leads on revenue; Tamara Chand leads on profit."
        tools = "Sean Miller, 25035.082\nTamara Chand, 19052.21"
        assert ungrounded_entities(answer, tools, VOCABULARY) == []

    def test_ordinary_prose_is_not_mistaken_for_a_record(self):
        answer = "Total revenue grew in the West, driven by Technology and Phones."
        assert ungrounded_entities(answer, "West: 100.0", VOCABULARY) == []

    def test_a_name_inside_a_longer_word_does_not_count(self):
        assert ungrounded_entities("Sean Millerson ordered", "", VOCABULARY) == []

    def test_products_are_covered_too(self):
        answer = "Most of it was one Canon imageCLASS 2200 Advanced Copier."
        assert ungrounded_entities(answer, "sub_category: Copiers", VOCABULARY) == [
            "Canon imageCLASS 2200 Advanced Copier",
        ]


class TestUngroundedClaims:
    def test_entities_are_reported_before_numbers(self):
        answer = "Sean Miller took 63% of it."
        assert ungrounded_claims(answer, "Tamara Chand: 100.0", VOCABULARY) == [
            "Sean Miller", "63",
        ]

    def test_a_fully_grounded_answer_is_empty(self):
        answer = ("By revenue the top customer is Sean Miller with $25,035 across "
                  "4 orders, though he is losing $1,983.")
        tools = "Sean Miller, 25035.082, -1983.4285, 4"
        assert ungrounded_claims(answer, tools, VOCABULARY) == []
