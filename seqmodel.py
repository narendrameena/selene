import math
import os

import numpy as np
import pandas as pd
from pyfaidx import Fasta
import tabix
import torch
from torch import nn
from torch import optim
from torch.autograd import Variable


torch.set_num_threads(32)


BASES = np.array(['A', 'G', 'C', 'T'])
DIR = "./data"  # TODO: REMOVE


def sequence_encoding(sequence):
    """Converts an input sequence to its one hot encoding.

    Parameters
    ----------
    sequence : str
        The input sequence of length N.

    Returns
    -------
    numpy.ndarray, dtype=bool
        The N-by-4 encoding of the sequence.
    """
    encoding = np.zeros((len(sequence), 4), np.bool_)
    for base, index in zip(sequence, range(len(sequence))):
        encoding[index, :] = BASES == base
    return encoding


class Genome:

    def __init__(self, fa_file):
        """Wrapper class around the pyfaix.Fasta class

        Parameters
        ----------
        fa_file : str
            Path to an indexed FASTA file.
            File should contain the target organism's genome sequence.

        Attributes
        ----------
        genome : Fasta
        chrs : list[str]
        """
        self.genome = Fasta(fa_file)
        self.chrs = sorted(self.genome.keys())

    def get_chr_sequence(self, chrom, strand='+'):
        """Get the genomic sequence given the chromosome
        and strand side.

        Parameters
        ----------
        chr : str
            e.g. "chr1"
        strand : {'+', '-'}, optional
            Default is '+'.

        Returns
        -------
        str
            The genomic sequence.

        Raises
        ------
        ValueError
            If the input char to `strand` is not one of the specified choices.
        """
        if strand == '+':
            return self.genome[chrom][:].seq
        elif strand == '-':
            return self.genome[chrom][:].reverse.complement.seq
        else:
            raise ValueError(
                "Strand must be one of '+' or '-'. Input was {0}".format(
                    strand))

    def get_sequence(self, chrom, start, end, strand='+'):
        """Get the genomic sequence given the chromosome, sequence start,
        sequence end, and strand side.

        Parameters
        ----------
        chrom : str
            e.g. "chr1".
        start : int
        end : int
        strand : {'+', '-'}, optional
            Default is '+'.

        Returns
        -------
        str
            The genomic sequence.

        Raises
        ------
        ValueError
            If the input char to `strand` is not one of the specified choices.
        """
        if start >= len(self.genome[chrom]):
            return ""
        end = min(end, len(self.genome[chrom]))

        if strand == '+':
            return self.genome[chrom][start:end].seq
        elif strand == '-':
            return self.genome[chrom][start:end].reverse.complement.seq
        else:
            raise ValueError(
                "Strand must be one of '+' or '-'. Input was {0}".format(
                    strand))


class GenomicFeatures:

    def __init__(self, dataset, features):
        """Stores the dataset specifying sequence regions and features.
        Accepts a tabix-indexed .bed file with the following columns,
        in order:
            [chrom, start (0-based), end, strand, feature]
        Additional columns following these 5 are acceptable.

        Parameters
        ----------
        dataset : str
            Path to the tabix-indexed dataset.
        features : list[str]
            The list of features (labels) we are interested in predicting.

        Attributes
        ----------
        data : tabix.open
        n_features : int
        features_map : dict
        """
        self.data = tabix.open(dataset)
        self.n_features = len(features)
        self.features_map = dict(
            [(feat, index) for index, feat in enumerate(features)])

    def is_positive(self, chrom, start, end, threshold=0.50):
        """Determines whether the (chrom, start, end) queried
        contains features that occupy over `threshold` * 100%
        of the (start, end) region. If so, this is a positive
        example.

        Parameters
        ----------
        chrom : str
            e.g. "chr1"
        start : int
        end : int
        threshold : [0.0, 1.0], float, optional
            Default is 0.50. The threshold specifies the proportion of
            the [`start`, `end`) window that needs to be covered by
            at least one feature for the example to be considered
            positive.

        Returns
        -------
        bool
            True if this meets the criterion for a positive example,
            False otherwise
        """
        try:
            rows = self.data.query(chrom, start, end)
            for row in rows:
                feat_start = int(row[1])
                feat_end = int(row[2])
                section_start = max(feat_start, start)
                section_end = min(feat_end, end)
                if section_end - section_start > (end - start - 1) * threshold:
                    return True
            return False
        except tabix.TabixError:
            return False

    def get_feature_data(self, chrom, start, end, strand='+'):
        """For a sequence of length L = `end` - `start`, return the features'
        one hot encoding corresponding to that region.
            e.g. for `n_features`, each position in that sequence will
            have a binary vector specifying whether each feature is
            present

        Parameters
        ----------
        chrom : str
            e.g. "chr1".
        start : int
        end : int
        strand : {'+', '-'}, optional
            Default is '+'.

        Returns
        -------
        numpy.ndarray
            shape = [L, n_features]

        Raises
        ------
        ValueError
            If the input char to `strand` is not one of the specified choices.
        """
        encoding = np.zeros((end - start, self.n_features))
        try:
            rows = self.data.query(chrom, start, end)
            if strand == '+':
                for row in rows:
                    # TODO: this could be a helper
                    feat_start = int(row[1]) - start
                    feat_end = int(row[2]) - start
                    feat_index = self.features_map[row[4]]
                    encoding[feat_start:feat_end, feat_index] = 1
            elif strand == '-':
                for row in rows:
                    feat_start = end - int(row[2])
                    feat_end = end - int(row[1])
                    feat_index = self.features_map[row[4]]
                    encoding[feat_start:feat_end, feat_index] = 1
            else:
                raise ValueError(
                    "Strand must be one of '+' or '-'. Input was {0}".format(
                        strand))
            return encoding
        except tabix.TabixError as e:
            print(">>>>> TABIX ERROR <<<<<")
            print(e)
            return encoding

class Sampler:

    MODES = ("all", "train", "test")
    EXPECTED_BED_COLS = (
        "chr", "start", "end", "strand", "feature", "metadata_index")
    USE_BED_COLS = (
        "chr", "start", "end", "strand")
    STRAND_SIDES = ('+', '-')

    def __init__(self, genome, genome_features, query_features,
                 holdout_chrs, radius=100, window_size=1001, mode="all"):
        """The class used to sample positive and negative examples from the
        genomic sequence. These examples are used during training/testing
        of the model.

        Parameters
        ----------
        genome : str
            Path to the indexed FASTA file of a target organism's complete
            genome sequence.
        genome_features : str
            Path to the .bed file that contains information about
            genomic features.
            File must have the following columns, in order:
                [chr, start (0-based), end, strand, feature, metadata_index]
        query_features : str
            Used for fast querying. Path to tabix-indexed .bed file that
            contains information about genomic features.
            (`genome_targets` is the uncompressed original)
        holdout_chrs : list[str]
            Specify chromosomes to hold out (used as the test dataset).
        radius : int, optional
            Default is 100. The bin is composed of
            <sequence length radius> + position + <sequence length radius>,
            i.e. 201 bp bin
        window_size : int, optional
            Default is 1001. The input sequence length.
            i.e. defaults result in 400 bp on either side of a 201 bp bin
        mode : {"all", "train", "test"}, optional
            Default is "all".

        Attributes
        ----------
        genome : Genome
        query_features : GenomicFeatures
        radius : int
        padding : int
            Should be identical on both sides
        mode : {"all", "train", "test"}


        Raises
        ------
        ValueError
            If the input str to `mode` is not one of the specified choices.
        """
        print(MODES)
        print(self.MODES)
        if mode not in self.MODES:
            raise ValueError("Mode must be one of {0}. Input was '{1}'.".format(MODES, mode))

        self.genome = Genome(genome)

        # used during the positive sampling step - get a random index from the
        # .bed file and the corresponding (chr, start, end, strand).
        self._features_df = pd.read_table(
            genome_data, header=None, names=self.EXPECTED_BED_COLS,
            usecols=self.USE_BED_COLS)
        # stores a copy of the .bed file that can be used to reset
        # `self._features_df` depending on what mode is specified.
        self._dup_features_df = pd.read_table(
            genome_data, header=None, names=self.EXPECTED_BED_COLS,
            usecols=self.USE_BED_COLS)
        self._training_indices = ~self._features_dataframe["chr"].isin(holdout_chrs)

        features = self._features_dataframe["feature"].unique()
        print(len(features))
        self.query_features = GenomicFeatures(query_features, features)

        # bin size = self.radius + 1 + self.radius
        self.radius = radius
        # the amount of padding is based on the window size and bin size.
        # we use the padding to incorporate sequence context around the
        # bin for which we have feature information.
        self.padding = 0
        # TODO: no error checking here yet.
        remaining_space = window_size - self.radius * 2 + 1
        if remaining_space > 0:
            self.padding = remaining_space / 2

        self.set_mode(mode)

        self._randcache = []
        self._strand_choices = ['+', '-']

    def set_mode(self, mode):
        if mode == "all":
            return

        if mode == "train":
            indices = np.asarray(self._training_indices)
        elif mode == "test":
            indices = ~np.asarray(self._training_indices)

        self._features_df = self._dup_features_df[indices]

    def _retrieve(self, chrom, position, strand,
                  sequence_only=False, verbose=False):
        """
        Parameters
        ----------
        chrom : char|str|int
        position : int
        strand : {'+', '-'}
        sequence_only : bool, optional
            Default is False.
        verbose : bool, optional
            Default is False.

        Returns
        -------
        np.ndarray | tuple(np.ndarray, np.ndarray)
            If `sequence_only`, returns the sequence encoding and nothing else.
            Otherwise, returns both the sequence encoding and the feature labels
            for the specified range.
        """
        if verbose:
            print("{0}, {1}, {2}".format(chrom, position, strand))
        sequence_start = position - self.radius - self.padding
        sequence_end = position + self.radius + self.padding + 1
        retrieved_sequence = sequence_encoding(
            self.genome.get_sequence(
                chrom, sequence_start, sequence_end, strand))
        if sequence_only:
            return retrieved_sequence
        else:
            retrieved_data = self.genome_features.get_feature_data(
                chrom, position - self.radius, position + self.radius + 1,
                strand, verbose=verbose)
            return (retrieved_sequence, retrieved_data)

    def sample_background(self, sequence_only=False, verbose=False):
        # TODO: documentation
        # TODO: random seed
        # should this be sample_negative now?
        if len(self._randcache) == 0:
            self._randcache = list(
                np.random.choice(self.genome.chrs, size=2000))
                #np.random.choice(
                #    self.genome.chrs, p=self.genome.chrs_distribution, size=2000))
        randchr = self._randcache.pop()
        randpos = np.random.choice(range(
            self.radius, len(self.genome.get(randchr) - self.radius)))
        randstrand = np.random.choice(self.strand_choices)
        # should query to make sure this is a true negative?
        is_positive = self.query_targets.is_positive(
            randchr, randpos - self.radius, randpos + self.radius + 1,
            self.radius, verbose=verbose)
        if is_positive:
            print("sampled background overlapped with positive examples")
            return self.sample_background(sequence_only, verbose)
        else:
            return self._retrieve(randchr, randpos, randstrand,
                sequence_only=sequence_only, verbose=verbose)

    def sample_positive(self, sequence_only=False, verbose=False):

        randind = np.random.randint(0, self._features_df.shape[0])
        row = self._features_df.iloc[randind]

        gene_length = row["end"] - row["start"]
        chrom = row["chr"]

        rand_in_gene = np.random.uniform() * gene_length
        position = int(
            row["start"] + rand_in_gene)

        strand = row["strand"]
        if strand == '.':
            strand = np.random.choice(self.strand_choices)

        if verbose:
            print chrom, position, strand
        return self._retrieve(chrom, position, strand,
                              sequence_only=sequence_only,
                              verbose=verbose)

    def sample_mixture(self, positive_proportion=0.5, sequence_only=False, verbose=False, padding=(0, 0)):
        """Gets a mixture of positive and background samples
        """
        if np.random.uniform() < positive_proportion:
            return self.sample_positive(sequence_only=sequence_only, verbose=verbose, padding=padding)
        else:
            return self.sample_background(sequence_only=sequence_only, verbose=verbose, padding=padding)

hiddenSizes = [100, 381]
n_lstm_layers = 2
rnn = nn.LSTM(input_size=4, hidden_size=hiddenSizes[0], num_layers=n_lstm_layers, batch_first=True, bidirectional=True)

# why is this hiddenSizes[0] * 2?
conv = nn.modules.container.Sequential(
    nn.Conv1d(hiddenSizes[0]*2, hiddenSizes[0]*2, 1),
    nn.ReLU(),
    nn.Conv1d(hiddenSizes[0]*2, hiddenSizes[1], 1))

model = [rnn, conv]
useCuda = True
if useCuda:
    for module in model:
        module.cuda()
n_features = 381
padding = (0, 0)
criterion = nn.BCELoss()
optimizers = [optim.SGD(module.parameters(), lr=0.05, momentum=0.95) for module in model]

def runBatch(batchSize=16, update=True, plot=False):
    window = sdata.radius * 2 + 1 + sum(padding)
    inputs = np.zeros((batchSize, window, len(BASES)))
    # should there be padding here?
    targets = np.zeros((batchSize, sdata.radius * 2 + 1, n_features))
    for i in range(batchSize):
        sequence, target = sdata.sample_mixture(0.5, padding=padding)
        if sequence.shape[0] != target.shape[0]:
            continue
        inputs[i, :, :] = sequence
        targets[i, :, :] = target  # score of just 1 ok?

    if useCuda:
        inputs = Variable(torch.Tensor(inputs).cuda(), requires_grad=True)
        targets = Variable(torch.Tensor(targets).cuda())
        h0 = Variable(torch.zeros(n_lstm_layers*2, batchSize, hiddenSizes[0]).cuda())
        c0 = Variable(torch.zeros(n_lstm_layers*2, batchSize, hiddenSizes[0]).cuda())
    else:
        inputs = Variable(torch.Tensor(inputs), requires_grad=True)
        targets = Variable(torch.Tensor(targets))
        h0 = Variable(torch.zeros(n_lstm_layers * 2, batchSize, hiddenSizes[0]))
        c0 = Variable(torch.zeros(n_lstm_layers * 2, batchSize, hiddenSizes[0]))

    outputs, hn = rnn(inputs, (h0, c0))
    outputs = conv(outputs.transpose(1,2)).transpose(1,2)

    loss = criterion(outputs,targets)

    if update:
        for module in model:
            module.zero_grad()
        loss.backward()
        for optimizer in optimizers:
            optimizer.step()

    if plot:
        plt.figure()
        plt.plot(outputs.data.numpy().flatten(),targets.data.numpy().flatten(),'.',alpha=0.2)
        plt.show()
    return loss.data[0]

sdata = SplicingDataset(
    os.path.join(DIR, "mm10_no_alt_analysis_set_ENCODE.fasta"),
    os.path.join(DIR, "reduced_agg_beds_1.bed.gz"),
    os.path.join(DIR, "reduced_agg_beds_1.bed"),
    #os.path.join(DIR, "splicejunc.database.bed.sorted.gz"),
    #os.path.join(DIR, "splicejunc.database.bed.sorted.gz"),
    #["5p", "3p"],
    ["chr8", "chr9"],
    radius=100,
    mode="train")

n_epochs = 3
for _ in range(n_epochs):
    sdata.set_mode("train")
    cumlossTrain = 0
    for _ in range(50):
        cumlossTrain = cumlossTrain + runBatch()

    sdata.set_mode("test")
    cumlossTest = 0
    for _ in range(5):
        cumlossTest = cumlossTest + runBatch(update=False)
    print("Train loss: %.5f, Test loss: %.5f." % (cumlossTrain, cumlossTest) )

torch.save(model,os.path.join(DIR, "models/test.mm10.cpu.model"))
