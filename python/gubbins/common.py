# encoding: utf-8
# Wellcome Trust Sanger Institute
# Copyright (C) 2013  Wellcome Trust Sanger Institute
# 
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#  
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#

import os
import sys
import shutil
import pkg_resources
import time
import subprocess
import re
import tempfile

import dendropy
from dendropy.calculate import treecompare

from Bio import AlignIO
from Bio import Phylo
from Bio import SeqIO
from Bio.Align import MultipleSeqAlignment
from Bio.Seq import Seq

from gubbins.PreProcessFasta import PreProcessFasta
from gubbins.ValidateFastaAlignment import ValidateFastaAlignment
from gubbins.treebuilders import FastTree, IQTree, RAxML
from gubbins import utils


def parse_and_run(input_args, program_description=""):
    """Main function of the Gubbins program"""
    start_time = time.time()
    current_directory = os.getcwd()
    printer = utils.VerbosePrinter(True, "\n")

    # Check if the Gubbins C-program is available. If so, print a welcome message. Otherwise exit.
    gubbins_exec = 'gubbins'
    if utils.which(gubbins_exec) is None:
        # Check if the Gubbins C-program is available in its source directory (for tests/Travis)
        gubbins_bundled_exec = os.path.abspath(os.path.join(current_directory, '../src/gubbins'))
        if utils.which(gubbins_bundled_exec) is None:
            sys.exit(gubbins_exec + " is not in your path")
        else:
            gubbins_exec = utils.replace_executable(gubbins_exec, gubbins_bundled_exec)
    program_version = ""
    try:
        program_version = str(pkg_resources.get_distribution(gubbins_exec).version)
    except pkg_resources.RequirementParseError:
        pass
    printer.print(["\n--- Gubbins " + program_version + " ---\n", program_description])

    # Initialize tree builder and ancestral sequence reconstructor; check if all required dependencies are available
    printer.print("\nChecking dependencies...")
    current_tree_name = input_args.starting_tree
    tree_file_names = []
    internal_node_label_prefix = "internal_"
    if input_args.tree_builder == "fasttree" or input_args.tree_builder == "hybrid":
        tree_builder = FastTree(input_args.verbose)
        sequence_reconstructor = RAxML(input_args.threads, input_args.raxml_model, internal_node_label_prefix,
                                       input_args.verbose)
        alignment_suffix = ".snp_sites.aln"
    elif input_args.tree_builder == "raxml":
        tree_builder = RAxML(input_args.threads, input_args.raxml_model, internal_node_label_prefix, input_args.verbose)
        sequence_reconstructor = tree_builder
        alignment_suffix = ".phylip"
    else:
        tree_builder = IQTree(input_args.threads, internal_node_label_prefix, input_args.verbose)
        sequence_reconstructor = tree_builder
        alignment_suffix = ".phylip"
    printer.print("...done. Run time: {:.2f} s".format(time.time() - start_time))

    # Check if the input files exist and have the right format
    printer.print("\nChecking input files...")
    if not os.path.exists(input_args.alignment_filename) \
            or not ValidateFastaAlignment(input_args.alignment_filename).is_input_fasta_file_valid():
        sys.exit("There input alignment file does not exist or has an invalid format")
    if input_args.starting_tree is not None and input_args.starting_tree != "" \
            and (not os.path.exists(input_args.starting_tree) or not is_starting_tree_valid(input_args.starting_tree)):
        sys.exit("The starting tree does not exist or has an invalid format")
    if input_args.starting_tree is not None and input_args.starting_tree != "" \
            and not do_the_names_match_the_fasta_file(input_args.starting_tree, input_args.alignment_filename):
        sys.exit("The names in the starting tree do not match the names in the alignment file")
    if number_of_sequences_in_alignment(input_args.alignment_filename) < 3:
        sys.exit("3 or more sequences are required.")

    # Check - and potentially correct - further input parameters
    check_and_fix_window_size(input_args)

    # Get the base filename
    (base_directory, base_filename) = os.path.split(input_args.alignment_filename)
    (basename, extension) = os.path.splitext(base_filename)
    if input_args.use_time_stamp:
        time_stamp = str(int(time.time()))
        basename = basename + "." + time_stamp
    snp_alignment_filename = base_filename + ".snp_sites.aln"
    gaps_alignment_filename = base_filename + ".gaps.snp_sites.aln"
    gaps_vcf_filename = base_filename + ".gaps.vcf"
    joint_sequences_filename = base_filename + ".seq.joint.aln"

    # Check if intermediate files from a previous run exist
    intermediate_files = [basename + ".iteration_"]
    if not input_args.no_cleanup:
        utils.delete_files(".", intermediate_files, "", input_args.verbose)
    if utils.do_files_exist(".", intermediate_files, "", input_args.verbose):
        sys.exit("Intermediate files from a previous run exist. Please rerun without the --no_cleanup option "
                 "to automatically delete them or with the --use_time_stamp to add a unique prefix.")
    printer.print("...done. Run time: {:.2f} s".format(time.time() - start_time))

    # Filter the input alignment and save as temporary alignment file
    printer.print("\nFiltering input alignment...")
    temp_working_dir = tempfile.mkdtemp(dir=os.getcwd())
    temp_alignment_filename = temp_working_dir + "/" + base_filename

    pre_process_fasta = PreProcessFasta(input_args.alignment_filename, input_args.verbose,
                                        input_args.filter_percentage)
    taxa_removed = pre_process_fasta.remove_duplicate_sequences_and_sequences_missing_too_much_data(
        temp_alignment_filename, input_args.remove_identical_sequences)
    input_args.alignment_filename = temp_alignment_filename

    # If a starting tree has been provided make sure that taxa filtered out in the previous step are removed from it
    if input_args.starting_tree:
        (tree_base_directory, tree_base_filename) = os.path.split(input_args.starting_tree)
        temp_starting_tree = temp_working_dir + '/' + tree_base_filename
        filter_out_removed_taxa_from_tree(input_args.starting_tree, temp_starting_tree, taxa_removed)
        input_args.starting_tree = temp_starting_tree
    printer.print("...done. Run time: {:.2f} s".format(time.time() - start_time))

    # Find all SNP sites with Gubbins
    gubbins_command = " ".join([gubbins_exec, input_args.alignment_filename])
    printer.print(["\nRunning Gubbins to detect SNPs...", gubbins_command])
    try:
        subprocess.check_call(gubbins_command, shell=True)
    except subprocess.SubprocessError:
        sys.exit("Gubbins crashed, please ensure you have enough free memory")
    printer.print("...done. Run time: {:.2f} s".format(time.time() - start_time))
    reconvert_fasta_file(snp_alignment_filename, snp_alignment_filename)
    reconvert_fasta_file(gaps_alignment_filename, base_filename + ".start")

    # Start the main loop
    printer.print("\nEntering the main loop.")
    for i in range(1, input_args.iterations+1):
        printer.print("\n*** Iteration " + str(i) + " ***")

        # 1.1. Construct the tree-building command depending on the iteration and employed options
        if i == 2 and input_args.tree_builder == "hybrid":
            # Switch to RAxML
            tree_builder = sequence_reconstructor
            alignment_suffix = ".phylip"

        if i == 1:
            previous_tree_name = input_args.starting_tree
            alignment_filename = base_filename + alignment_suffix
        else:
            previous_tree_name = current_tree_name
            alignment_filename = previous_tree_name + alignment_suffix

        current_basename = basename + ".iteration_" + str(i)
        current_tree_name = current_basename + ".tre"
        if previous_tree_name:
            tree_building_command = tree_builder.tree_building_command(
                os.path.abspath(alignment_filename), os.path.abspath(previous_tree_name), current_basename)
        else:
            tree_building_command = tree_builder.tree_building_command(
                os.path.abspath(alignment_filename), "", current_basename)
        built_tree = temp_working_dir + "/" + tree_builder.tree_prefix + current_basename + tree_builder.tree_suffix

        # 1.2. Construct the phylogenetic tree
        if input_args.starting_tree is not None and i == 1:
            printer.print("\nCopying the starting tree...")
            shutil.copyfile(input_args.starting_tree, current_tree_name)
        else:
            printer.print(["\nConstructing the phylogenetic tree with " + tree_builder.executable + "...",
                           tree_building_command])
            os.chdir(temp_working_dir)
            try:
                subprocess.check_call(tree_building_command, shell=True)
            except subprocess.SubprocessError:
                sys.exit("Failed while building the tree.")
            os.chdir(current_directory)
            shutil.copyfile(built_tree, current_tree_name)
        printer.print("...done. Run time: {:.2f} s".format(time.time() - start_time))

        # 2. Re-root the tree
        reroot_tree(str(current_tree_name), input_args.outgroup)
        temp_rooted_tree = temp_working_dir + "/" + current_tree_name + ".rooted"
        if input_args.tree_builder == "iqtree":
            shutil.copyfile(current_tree_name, temp_rooted_tree)
        else:
            root_tree(current_tree_name, temp_rooted_tree)

        # 3.1. Construct the command for ancestral state reconstruction depending on the iteration and employed options
        ancestral_sequence_basename = current_basename + ".internal"
        sequence_reconstruction_command = sequence_reconstructor.internal_sequence_reconstruction_command(
            os.path.abspath(base_filename + alignment_suffix), os.path.abspath(temp_rooted_tree),
            ancestral_sequence_basename)
        raw_internal_sequence_filename \
            = temp_working_dir + "/" + sequence_reconstructor.asr_prefix \
            + ancestral_sequence_basename + sequence_reconstructor.asr_suffix
        processed_internal_sequence_filename = temp_working_dir + "/" + ancestral_sequence_basename + ".aln"
        raw_internal_rooted_tree_filename \
            = temp_working_dir + "/" + sequence_reconstructor.asr_tree_prefix \
            + ancestral_sequence_basename + sequence_reconstructor.asr_tree_suffix

        # 3.2. Reconstruct the ancestral sequence
        printer.print(["\nReconstructing ancestral sequences with " + sequence_reconstructor.executable + "...",
                       sequence_reconstruction_command])
        os.chdir(temp_working_dir)
        try:
            subprocess.check_call(sequence_reconstruction_command, shell=True)
        except subprocess.SubprocessError:
            sys.exit("Failed while reconstructing the ancestral sequences.")
        os.chdir(current_directory)

        # 3.3. Join ancestral sequences with given sequences
        current_tree_name_with_internal_nodes = current_tree_name + ".internal"
        sequence_reconstructor.convert_raw_ancestral_states_to_fasta(raw_internal_sequence_filename,
                                                                     processed_internal_sequence_filename)
        concatenate_fasta_files([snp_alignment_filename, processed_internal_sequence_filename],
                                joint_sequences_filename)
        transfer_internal_node_labels_to_tree(raw_internal_rooted_tree_filename, temp_rooted_tree,
                                              current_tree_name_with_internal_nodes, sequence_reconstructor)
        printer.print("...done. Run time: {:.2f} s".format(time.time() - start_time))

        # 4. Reinsert gaps (cp15 note: something is wonky here, the process is at the very least terribly inefficient)
        printer.print("\nReinserting gaps into the alignment...")
        shutil.copyfile(base_filename + ".start", gaps_alignment_filename)
        reinsert_gaps_into_fasta_file(joint_sequences_filename, gaps_vcf_filename, gaps_alignment_filename)
        if not os.path.exists(gaps_alignment_filename) \
                or not ValidateFastaAlignment(gaps_alignment_filename).is_input_fasta_file_valid():
            sys.exit("There is a problem with your FASTA file after running internal sequence reconstruction. "
                     "Please check this intermediate file is valid: " + gaps_alignment_filename)
        printer.print("...done. Run time: {:.2f} s".format(time.time() - start_time))

        # 5. Detect recombination sites with Gubbins (cp15 note: copy file with internal nodes back and forth to
        # ensure all created files have the desired name structure and to avoid fiddling with the Gubbins C program)
        shutil.copyfile(current_tree_name_with_internal_nodes, current_tree_name)
        gubbins_command = create_gubbins_command(
            gubbins_exec, gaps_alignment_filename, gaps_vcf_filename, current_tree_name,
            input_args.alignment_filename, input_args.min_snps, input_args.min_window_size, input_args.max_window_size)
        printer.print(["\nRunning Gubbins to detect recombinations...", gubbins_command])
        try:
            subprocess.check_call(gubbins_command, shell=True)
        except subprocess.SubprocessError:
            sys.exit("Failed while running Gubbins. Please ensure you have enough free memory")
        printer.print("...done. Run time: {:.2f} s".format(time.time() - start_time))
        shutil.copyfile(current_tree_name, current_tree_name_with_internal_nodes)

        # 6. Check for convergence
        printer.print("\nChecking for convergence...")
        remove_internal_node_labels_from_tree(current_tree_name_with_internal_nodes, current_tree_name)
        tree_file_names.append(current_tree_name)
        if i > 1:
            if input_args.converge_method == 'recombination':
                current_recomb_file, previous_recomb_files = get_recombination_files(tree_file_names)
                if have_recombinations_been_seen_before(current_recomb_file, previous_recomb_files):
                    printer.print("Convergence after " + str(i) + " iterations: Recombinations observed before.")
                    break
            else:
                if has_tree_been_seen_before(tree_file_names, input_args.converge_method):
                    printer.print("Convergence after " + str(i) + " iterations: Tree observed before.")
                    break
        printer.print("...done. Run time: {:.2f} s".format(time.time() - start_time))
    else:
        printer.print("Maximum number of iterations (" + str(input_args.iterations) + ") reached.")
    printer.print("\nExiting the main loop.")

    # Create the final output
    printer.print("\nCreating the final output...")
    if input_args.prefix is None:
        input_args.prefix = basename
    output_filenames_to_final_filenames = translation_of_filenames_to_final_filenames(
        current_tree_name, input_args.prefix)
    utils.rename_files(output_filenames_to_final_filenames)

    # Cleanup intermediate files
    if not input_args.no_cleanup:
        shutil.rmtree(temp_working_dir)
        utils.delete_files(".", tree_file_names[:-1], intermediate_files_regex(), input_args.verbose)
        utils.delete_files(".", [base_filename], starting_files_regex(), input_args.verbose)
    printer.print("...finished. Total run time: {:.2f} s".format(time.time() - start_time))


def create_gubbins_command(gubbins_exec, alignment_filename, vcf_filename, current_tree_name,
                           original_alignment_filename, min_snps, min_window_size, max_window_size):
    command = [gubbins_exec, "-r", "-v", vcf_filename, "-a", str(min_window_size),
               "-b", str(max_window_size), "-f", original_alignment_filename, "-t", current_tree_name,
               "-m", str(min_snps), alignment_filename]
    return " ".join(command)


def number_of_sequences_in_alignment(filename):
    return len(get_sequence_names_from_alignment(filename))


def get_sequence_names_from_alignment(filename):
    sequence_names = []
    with open(filename, "r") as handle:
        for record in SeqIO.parse(handle, "fasta"):
            sequence_names.append(record.id)
    return sequence_names


def is_starting_tree_valid(starting_tree):
    try:
        Phylo.read(starting_tree, 'newick')
        dendropy.Tree.get_from_path(starting_tree, 'newick', preserve_underscores=True)
    except Exception:
        print("Error with the input starting tree: Is it a valid Newick file?")
        return False
    return True


def do_the_names_match_the_fasta_file(starting_tree, alignment_filename):
    with open(alignment_filename, "r") as input_handle:
        alignments = AlignIO.parse(input_handle, "fasta")
        sequence_names = {}
        for alignment in alignments:
            for record in alignment:
                sequence_names[record.name] = 1
        input_handle.close()

        tree = dendropy.Tree.get_from_path(starting_tree, 'newick', preserve_underscores=True)

        leaf_nodes = tree.leaf_nodes()
        for i, lf in enumerate(leaf_nodes):
            if not leaf_nodes[i].taxon.label in sequence_names:
                print("Error: A taxon referenced in the starting tree is not found in the input fasta file")
                return False
    return True


def check_and_fix_window_size(input_args):
    if input_args.min_window_size < 3:
        input_args.min_window_size = 3
    if input_args.max_window_size > 1000000:
        input_args.max_window_size = 1000000
    if input_args.min_window_size > input_args.max_window_size:
        input_args.max_window_size, input_args.min_window_size = input_args.min_window_size, input_args.max_window_size


def reconvert_fasta_file(input_filename, output_filename):
    with open(input_filename, "r") as input_handle:
        alignment = AlignIO.read(input_handle, "fasta")
    with open(output_filename, "w+") as output_handle:
        AlignIO.write(alignment, output_handle, "fasta")


def concatenate_fasta_files(input_filenames, output_filename):
    alignments = []
    for input_filename in input_filenames:
        with open(input_filename, "r") as input_handle:
            alignments.append(AlignIO.read(input_handle, "fasta"))

    with open(output_filename, "w+") as output_handle:
        for alignment in alignments:
            AlignIO.write(alignment, output_handle, "fasta")


def starting_files_regex():
    return "\\.(gaps|snp_sites|phylip|vcf|start|seq)"


def intermediate_files_regex():
    return "($|\\.(gff|vcf|snp_sites|branch_snps|phylip|stats|tab|internal))"


def root_tree(input_filename, output_filename):
    # split bi nodes and root tree
    tree = dendropy.Tree.get_from_path(input_filename, 'newick', preserve_underscores=True)
    split_all_non_bi_nodes(tree.seed_node)
    output_tree_string = tree_as_string(tree, suppress_internal=False, suppress_rooting=False)
    with open(output_filename, 'w+') as output_file:
        output_file.write(output_tree_string.replace('\'', ''))


def reroot_tree(tree_name, outgroups):
    if outgroups:
        reroot_tree_with_outgroup(tree_name, outgroups.split(','))
    else:
        reroot_tree_at_midpoint(tree_name)


def reroot_tree_with_outgroup(tree_name, outgroups):
    clade_outgroups = get_monophyletic_outgroup(tree_name, outgroups)
    outgroups = [{'name': taxon_name} for taxon_name in clade_outgroups]

    tree = Phylo.read(tree_name, 'newick')
    tree.root_with_outgroup(*outgroups)
    Phylo.write(tree, tree_name, 'newick')

    tree = dendropy.Tree.get_from_path(tree_name, 'newick', preserve_underscores=True)
    tree.deroot()
    tree.update_bipartitions()
    output_tree_string = tree_as_string(tree, suppress_internal=False)
    with open(tree_name, 'w+') as output_file:
        output_file.write(output_tree_string.replace('\'', ''))


def reroot_tree_at_midpoint(tree_name):
    tree = dendropy.Tree.get_from_path(tree_name, 'newick', preserve_underscores=True)
    split_all_non_bi_nodes(tree.seed_node)
    tree.update_bipartitions()
    tree.reroot_at_midpoint()
    tree.deroot()
    tree.update_bipartitions()
    output_tree_string = tree_as_string(tree, suppress_internal=False)
    with open(tree_name, 'w+') as output_file:
        output_file.write(output_tree_string.replace('\'', ''))


def filter_out_removed_taxa_from_tree(input_filename, output_filename, taxa_removed):
    tree = dendropy.Tree.get_from_path(input_filename, 'newick', preserve_underscores=True)
    tree.prune_taxa_with_labels(taxa_removed, update_bipartitions=True)
    tree.prune_leaves_without_taxa(update_bipartitions=True)
    tree.deroot()
    output_tree_string = tree_as_string(tree)
    with open(output_filename, 'w+') as output_file:
        output_file.write(output_tree_string.replace('\'', ''))


def tree_as_string(tree, suppress_internal=True, suppress_rooting=True):

    return tree.as_string(
        schema='newick',
        suppress_leaf_taxon_labels=False,
        suppress_leaf_node_labels=True,
        suppress_internal_taxon_labels=suppress_internal,
        suppress_internal_node_labels=suppress_internal,
        suppress_rooting=suppress_rooting,
        suppress_edge_lengths=False,
        unquoted_underscores=True,
        preserve_spaces=False,
        store_tree_weights=False,
        suppress_annotations=True,
        annotations_as_nhx=False,
        suppress_item_comments=True,
        node_label_element_separator=' '
    )


def split_all_non_bi_nodes(node):
    if node.is_leaf():
        return None
    elif len(node.child_nodes()) > 2:
        split_child_nodes(node)

    for child_node in node.child_nodes():
        split_all_non_bi_nodes(child_node)

    return None


def split_child_nodes(node):
    all_child_nodes = node.child_nodes()
    # skip over the first node
    first_child = all_child_nodes.pop()
    # create a placeholder node to hang everything else off
    new_child_node = node.new_child(edge_length=0)
    # move the subtree into the placeholder
    new_child_node.set_child_nodes(all_child_nodes)
    # probably not really necessary
    node.set_child_nodes((first_child, new_child_node))


def get_monophyletic_outgroup(tree_name, outgroups):
    if len(outgroups) == 1:
        return outgroups

    tree = dendropy.Tree.get_from_path(tree_name, 'newick', preserve_underscores=True)
    tree.deroot()
    tree.update_bipartitions()

    for leaf_node in tree.mrca(taxon_labels=outgroups).leaf_nodes():
        if leaf_node.taxon.label not in outgroups:
            print("Your outgroups do not form a clade.\n  Using the first taxon " + str(outgroups[0]) +
                  " as the outgroup.\n  Taxon " + str(leaf_node.taxon.label) +
                  " is in the clade but not in your list of outgroups.")
            return [outgroups[0]]

    return outgroups


def transfer_internal_node_labels_to_tree(source_tree_filename, destination_tree_filename, output_tree_filename,
                                          sequence_reconstructor):
    source_tree = dendropy.Tree.get_from_path(source_tree_filename, 'newick', preserve_underscores=True)
    source_internal_node_labels = []
    for source_internal_node in source_tree.internal_nodes():
        if source_internal_node.label:
            source_internal_node_labels.append(source_internal_node.label)
        else:
            source_internal_node_labels.append('')

    destination_tree = dendropy.Tree.get_from_path(destination_tree_filename, 'newick', preserve_underscores=True)
    for index, destination_internal_node in enumerate(destination_tree.internal_nodes()):
        new_label = sequence_reconstructor.replace_internal_node_label(str(source_internal_node_labels[index]))
        destination_internal_node.label = None
        destination_internal_node.taxon = dendropy.Taxon(new_label)

    output_tree_string = tree_as_string(destination_tree, suppress_internal=False, suppress_rooting=False)
    with open(output_tree_filename, 'w+') as output_file:
        output_file.write(output_tree_string.replace('\'', ''))


def remove_internal_node_labels_from_tree(input_filename, output_filename):
    tree = dendropy.Tree.get_from_path(input_filename, 'newick', preserve_underscores=True)
    output_tree_string = tree_as_string(tree)
    with open(output_filename, 'w+') as output_file:
        output_file.write(output_tree_string.replace('\'', ''))


def reinsert_gaps_into_fasta_file(input_fasta_filename, input_vcf_file, output_fasta_filename):
    # find out where the gaps are located
    # PyVCF removed for performance reasons
    with open(input_vcf_file) as vcf_file:

        sample_names = []
        gap_position = []
        gap_alt_base = []

        for vcf_line in vcf_file:
            if re.match('^#CHROM', vcf_line) is not None:
                sample_names = vcf_line.rstrip().split('\t')[9:]
            elif re.match('^\d', vcf_line) is not None:
                # If the alternate is only a gap it wont have a base in this column
                if re.match('^([^\t]+\t){3}([ACGTacgt])\t([^ACGTacgt])\t', vcf_line) is not None:
                    m = re.match('^([^\t]+\t){3}([ACGTacgt])\t([^ACGTacgt])\t', vcf_line)
                    gap_position.append(1)
                    gap_alt_base.append(m.group(2))
                elif re.match('^([^\t]+\t){3}([^ACGTacgt])\t([ACGTacgt])\t', vcf_line) is not None:
                    # sometimes the ref can be a gap only
                    m = re.match('^([^\t]+\t){3}([^ACGTacgt])\t([ACGTacgt])\t', vcf_line)
                    gap_position.append(1)
                    gap_alt_base.append(m.group(3))
                else:
                    gap_position.append(0)
                    gap_alt_base.append('-')

        gapped_alignments = []
        # interleave gap only and snp bases
        with open(input_fasta_filename, "r") as input_handle:
            alignments = AlignIO.parse(input_handle, "fasta")
            for alignment in alignments:
                for record in alignment:
                    inserted_gaps = []
                    if record.id in sample_names:
                        # only apply to internal nodes
                        continue
                    gap_index = 0
                    for input_base in record.seq:
                        while gap_index < len(gap_position) and gap_position[gap_index] == 1:
                            inserted_gaps.append(gap_alt_base[gap_index])
                            gap_index += 1
                        if gap_index < len(gap_position):
                            inserted_gaps.append(input_base)
                            gap_index += 1

                    while gap_index < len(gap_position):
                        inserted_gaps.append(gap_alt_base[gap_index])
                        gap_index += 1

                    record.seq = Seq(''.join(inserted_gaps))
                    gapped_alignments.append(record)

        with open(output_fasta_filename, "a") as output_handle:
            AlignIO.write(MultipleSeqAlignment(gapped_alignments), output_handle, "fasta")
            output_handle.close()
    return


def get_recombination_files(basenames):
    previous_files = []
    for name in basenames:
        previous_files.append(name + ".tab")
    current_file = previous_files.pop()
    return current_file, previous_files


def have_recombinations_been_seen_before(current_file, previous_files):
    if not os.path.exists(current_file):
        return False
    current_file_recombinations = extract_recombinations_from_embl(current_file)

    for previous_file in previous_files:
        if not os.path.exists(previous_file):
            continue
        previous_file_recombinations = extract_recombinations_from_embl(previous_file)
        if current_file_recombinations == previous_file_recombinations:
            return True
    return False


def extract_recombinations_from_embl(filename):
    with open(filename, "r") as fh:
        sequences_to_coords = {}
        start_coord = -1
        end_coord = -1
        for line in fh:
            search_obj = re.search('misc_feature    ([\d]+)..([\d]+)$', line)
            if search_obj is not None:
                start_coord = int(search_obj.group(1))
                end_coord = int(search_obj.group(2))
                continue

            if start_coord >= 0 and end_coord >= 0:
                search_taxa = re.search('taxa\=\"([^"]+)\"', line)
                if search_taxa is not None:
                    taxa_names = search_taxa.group(1).strip().split(' ')
                    for taxa_name in taxa_names:
                        if taxa_name in sequences_to_coords:
                            sequences_to_coords[taxa_name].append([start_coord, end_coord])
                        else:
                            sequences_to_coords[taxa_name] = [[start_coord, end_coord]]

                    start_coord = -1
                    end_coord = -1
                continue
        fh.close()
    return sequences_to_coords


def has_tree_been_seen_before(tree_file_names, converge_method):
    if len(tree_file_names) <= 2:
        return False

    tree_files_which_exist = []
    for tree_file_name in tree_file_names:
        if os.path.exists(tree_file_name):
            tree_files_which_exist.append(tree_file_name)

    for tree_file_name in tree_files_which_exist:
        if tree_file_name is not tree_files_which_exist[-1]:
            if converge_method == 'weighted_robinson_foulds':
                current_rf_distance = robinson_foulds_distance(
                    tree_file_name, tree_files_which_exist[-1])
                if current_rf_distance == 0.0:
                    return True
            else:
                current_rf_distance = symmetric_difference(
                    tree_file_name, tree_files_which_exist[-1])
                if current_rf_distance == 0.0:
                    return True

    return False


def robinson_foulds_distance(input_tree_name, output_tree_name):
    tns = dendropy.TaxonNamespace()
    input_tree = dendropy.Tree.get_from_path(input_tree_name, 'newick', taxon_namespace=tns)
    output_tree = dendropy.Tree.get_from_path(output_tree_name, 'newick', taxon_namespace=tns)
    input_tree.encode_bipartitions()
    output_tree.encode_bipartitions()
    return dendropy.calculate.treecompare.weighted_robinson_foulds_distance(input_tree, output_tree)


def symmetric_difference(input_tree_name, output_tree_name):
    tns = dendropy.TaxonNamespace()
    input_tree = dendropy.Tree.get_from_path(input_tree_name, 'newick', taxon_namespace=tns)
    output_tree = dendropy.Tree.get_from_path(output_tree_name, 'newick', taxon_namespace=tns)
    input_tree.encode_bipartitions()
    output_tree.encode_bipartitions()
    return dendropy.calculate.treecompare.symmetric_difference(input_tree, output_tree)


def translation_of_filenames_to_final_filenames(input_prefix, output_prefix):
    input_names_to_output_names = {
        str(input_prefix) + ".vcf":             str(output_prefix) + ".summary_of_snp_distribution.vcf",
        str(input_prefix) + ".branch_snps.tab": str(output_prefix) + ".branch_base_reconstruction.embl",
        str(input_prefix) + ".tab":             str(output_prefix) + ".recombination_predictions.embl",
        str(input_prefix) + ".gff":             str(output_prefix) + ".recombination_predictions.gff",
        str(input_prefix) + ".stats":           str(output_prefix) + ".per_branch_statistics.csv",
        str(input_prefix) + ".snp_sites.aln":   str(output_prefix) + ".filtered_polymorphic_sites.fasta",
        str(input_prefix) + ".phylip":          str(output_prefix) + ".filtered_polymorphic_sites.phylip",
        str(input_prefix) + ".internal":        str(output_prefix) + ".node_labelled.final_tree.tre",
        str(input_prefix):                      str(output_prefix) + ".final_tree.tre"
    }
    return input_names_to_output_names
