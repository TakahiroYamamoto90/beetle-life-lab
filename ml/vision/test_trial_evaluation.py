"""Metrics contract checks; no hardware or biological performance measured."""
import unittest

from evaluate_trial import evaluation_limitations, summarize


class TrialEvaluationTests(unittest.TestCase):
    def test_binary_confusion_orientation_and_precision_recall(self):
        result = summarize([0, 0, 1, 1], [0, 1, 0, 1])
        self.assertEqual(result["confusion_rows_actual_columns_predicted"], [[1, 1], [1, 1]])
        self.assertEqual(result["visible_precision"], .5)
        self.assertEqual(result["visible_recall"], .5)
        self.assertEqual(result["false_positive_count"], 1)
        self.assertEqual(result["false_negative_count"], 1)
        self.assertTrue(result["both_classes_present"])

    def test_positive_only_batch_is_not_a_both_class_evaluation(self):
        result = summarize([1, 1], [1, 0])
        self.assertFalse(result["both_classes_present"])
        self.assertEqual(result["visible_recall"], .5)
        result = summarize([0, 0], [0, 0])
        self.assertIsNone(result["visible_precision"])
        self.assertIsNone(result["visible_recall"])

    def test_empty_mismatched_or_invalid_ids_fail(self):
        for actual, predicted in (([], []), ([0], []), ([2], [1]), ([1], [-1])):
            with self.assertRaises(ValueError):
                summarize(actual, predicted)

    def test_single_class_warning_depends_on_actual_added_batch(self):
        only_positive = summarize([1, 1], [1, 0])
        mixed = summarize([0, 1], [0, 1])
        self.assertTrue(any("only one class" in note for note in evaluation_limitations(only_positive)))
        self.assertFalse(any("only one class" in note for note in evaluation_limitations(mixed)))
        self.assertFalse(any("only one class" in note for note in evaluation_limitations(None)))


if __name__ == "__main__":
    unittest.main()
