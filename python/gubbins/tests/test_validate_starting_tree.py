#! /usr/bin/env python3
# encoding: utf-8

"""
Tests the validation of the starting newick tree.
"""

import unittest
from gubbins import common


class TestValidationOfStartingTree(unittest.TestCase):

    def test_are_all_names_unique(self):
        assert not common.is_starting_tree_valid('gubbins/tests/data/duplicate_names_in_tree.tre')

    def test_is_it_a_valid_newick_tree(self):
        assert not common.is_starting_tree_valid('gubbins/tests/data/invalid_newick_tree.tre')
        assert common.is_starting_tree_valid('gubbins/tests/data/valid_newick_tree.tre')

    def test_do_the_names_match_the_fasta_file(self):
        assert common.do_the_names_match_the_fasta_file('gubbins/tests/data/valid_newick_tree.tre',
                                                        'gubbins/tests/data/valid_newick_tree.aln')


if __name__ == "__main__":
    unittest.main()
