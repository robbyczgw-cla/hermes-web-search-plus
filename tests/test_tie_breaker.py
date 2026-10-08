import unittest

from wsp_core.quality import _choose_tie_winner


class TieBreakerTests(unittest.TestCase):
    PRIORITY = ["tavily", "querit", "exa",
                "brave", "serper", "you", "searxng"]

    def test_single_winner_returns_same(self):
        self.assertEqual(
            _choose_tie_winner("any query", ["brave"], self.PRIORITY),
            "brave",
        )

    def test_same_query_same_winner(self):
        winners = ["brave", "serper"]
        results = {
            _choose_tie_winner("weather graz", winners, self.PRIORITY)
            for _ in range(100)
        }
        self.assertEqual(len(results), 1)

    def test_distribution_across_queries(self):
        winners = ["brave", "serper"]
        queries = [f"query number {i}" for i in range(200)]
        picks = [_choose_tie_winner(q, winners, self.PRIORITY)
                 for q in queries]
        brave_ratio = picks.count("brave") / len(picks)
        self.assertGreater(brave_ratio, 0.35)
        self.assertLess(brave_ratio, 0.65)

    def test_deterministic_without_priority(self):
        winners = ["serper", "brave"]
        result1 = _choose_tie_winner("test query", winners, [])
        result2 = _choose_tie_winner("test query", winners, [])
        self.assertEqual(result1, result2)
        self.assertIn(result1, winners)


if __name__ == "__main__":
    unittest.main()
