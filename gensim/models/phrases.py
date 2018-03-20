#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Licensed under the GNU LGPL v2.1 - http://www.gnu.org/licenses/lgpl.html

"""Automatically detect common phrases (multiword expressions) from a stream of sentences.

The phrases are collocations (frequently co-occurring tokens). See `Tomas Mikolov, Ilya Sutskever, Kai Chen,
Greg Corrado, and Jeffrey Dean. Distributed Representations of Words and Phrases and their Compositionality.
In Proceedings of NIPS, 2013.
<https://papers.nips.cc/paper/5021-distributed-representations-of-words-and-phrases-and-their-compositionality.pdf>`_
for the exact formula.

For example, if your input stream (=an iterable, with each value a list of token strings) looks like:

>>> print(list(sentence_stream))
[[u'the', u'mayor', u'of', u'new', u'york', u'was', u'there'],
 [u'machine', u'learning', u'can', u'be', u'useful', u'sometimes'],
 ...,
]

you'd train the detector with:

>>> phrases = Phrases(sentence_stream)

and then create a performant Phraser object to transform any sentence (list of token strings)
using the standard gensim syntax:

>>> bigram = Phraser(phrases)
>>> sent = [u'the', u'mayor', u'of', u'new', u'york', u'was', u'there']
>>> print(bigram[sent])
[u'the', u'mayor', u'of', u'new_york', u'was', u'there']

(note `new_york` became a single token). As usual, you can also transform an entire
sentence stream using:

>>> print(list(bigram[any_sentence_stream]))
[[u'the', u'mayor', u'of', u'new_york', u'was', u'there'],
 [u'machine_learning', u'can', u'be', u'useful', u'sometimes'],
 ...,
]

You can also continue updating the collocation counts with new sentences, by:

>>> bigram.add_vocab(new_sentence_stream)

These **phrase streams are meant to be used during text preprocessing, before
converting the resulting tokens into vectors using `Dictionary`**. See the
:mod:`gensim.models.word2vec` module for an example application of using phrase detection.

The detection can also be **run repeatedly**, to get phrases longer than
two tokens (e.g. `new_york_times`):

>>> trigram = Phrases(bigram[sentence_stream])
>>> sent = [u'the', u'new', u'york', u'times', u'is', u'a', u'newspaper']
>>> print(trigram[bigram[sent]])
[u'the', u'new_york_times', u'is', u'a', u'newspaper']

The common_terms parameter add a way to give special treatment to common terms (aka stop words)
such that their presence between two words
won't prevent bigram detection.
It allows to detect expressions like "bank of america" or "eye of the beholder".

>>> common_terms = ["of", "with", "without", "and", "or", "the", "a"]
>>> ct_phrases = Phrases(sentence_stream, common_terms=common_terms)

The phraser will of course inherit the common_terms from Phrases.

>>> ct_bigram = Phraser(ct_phrases)
>>> sent = [u'the', u'mayor', u'shows', u'his', u'lack', u'of', u'interest']
>>> print(bigram[sent])
[u'the', u'mayor', u'shows', u'his', u'lack_of_interest']

"""

import sys
import os
import logging
import warnings
from collections import defaultdict
import functools as ft
import itertools as it
from math import log
import pickle
import six

from six import iteritems, string_types, PY2, next

from gensim import utils, interfaces

if PY2:
    from inspect import getargspec
else:
    from inspect import getfullargspec as getargspec

logger = logging.getLogger(__name__)


def _is_single(obj):
    """Check whether `obj` is a single document or an entire corpus.

    Parameters
    ----------
    obj : object

    Return
    ------
    (bool, object)
        (is_single, new) tuple, where `new` yields the same sequence as `obj`.

    Notes
    -----
    `obj` is a single document if it is an iterable of strings. It is a corpus if it is an iterable of documents.

    """
    obj_iter = iter(obj)
    temp_iter = obj_iter
    try:
        peek = next(obj_iter)
        obj_iter = it.chain([peek], obj_iter)
    except StopIteration:
        # An empty object is a single document
        return True, obj
    if isinstance(peek, string_types):
        # It's a document, return the iterator
        return True, obj_iter
    if temp_iter == obj:
        # Checking for iterator to the object
        return False, obj_iter
    else:
        # If the first item isn't a string, assume obj is a corpus
        return False, obj


class SentenceAnalyzer(object):

    def score_item(self, worda, wordb, components, scorer):
        """Get sentence statistics.

        Parameters
        ----------
        worda : str
            First word for comparison. Should be unicode string.
        wordb : str
            Second word for comparison. Should be unicode string.
        components : TODO
        scorer : {'default', 'npmi'}
            Scorer function, as given to :class:`~gensim.models.phrases.Phrases`.

        Return
        ------
        {'default', 'npmi'}
            Scorer function with filled `worda`, `wordb` & `bigram` counters.

        """
        vocab = self.vocab
        if worda in vocab and wordb in vocab:
            bigram = self.delimiter.join(components)
            if bigram in vocab:
                return scorer(
                    worda_count=float(vocab[worda]),
                    wordb_count=float(vocab[wordb]),
                    bigram_count=float(vocab[bigram]))
        return -1

    def analyze_sentence(self, sentence, threshold, common_terms, scorer):
        """Analyze a sentence.

        Parameters
        ----------
        sentence : list of str
            Token list representing the sentence to be analyzed.
        threshold : int
            The minimum score for a bigram to be taken into account.
        common_terms : list of object
            List of common terms, they have a special treatment.
        scorer : {'default', 'npmi'}
            Scorer function, as given to :class:`~gensim.models.phrases.Phrases`.

        """
        s = [utils.any2utf8(w) for w in sentence]
        last_uncommon = None
        in_between = []
        # adding None is a trick that helps getting an automatic happy ending
        # has it won't be a common_word, nor score
        for word in s + [None]:
            is_common = word in common_terms
            if not is_common and last_uncommon:
                chain = [last_uncommon] + in_between + [word]
                # test between last_uncommon
                score = self.score_item(
                    worda=last_uncommon,
                    wordb=word,
                    components=chain,
                    scorer=scorer,
                )
                if score > threshold:
                    yield (chain, score)
                    last_uncommon = None
                    in_between = []
                else:
                    # release words individually
                    for w in it.chain([last_uncommon], in_between):
                        yield (w, None)
                    in_between = []
                    last_uncommon = word
            elif not is_common:
                last_uncommon = word
            else:  # common term
                if last_uncommon:
                    # wait for uncommon resolution
                    in_between.append(word)
                else:
                    yield (word, None)


class PhrasesTransformation(interfaces.TransformationABC):

    @classmethod
    def load(cls, *args, **kwargs):
        """Load a previously saved :class:`~gensim.models.phrases.Phrases` /
        :class:`~gensim.models.phrases.Phraser` class. Handles backwards compatibility from older
        :class:`~gensim.models.phrases.Phrases` / :class:`~gensim.models.phrases.Phraser`
        versions which did not support pluggable scoring functions. Otherwise, relies on utils.load.

        Parameters
        ----------
        args : object
            Sequence of arguments, see :class:`~gensim.models.phrases.Phrases` for more information.
        kwargs : object
            Sequence of arguments, see :class:`~gensim.models.phrases.Phrases` for more information.

        """
        model = super(PhrasesTransformation, cls).load(*args, **kwargs)
        # update older models
        # if no scoring parameter, use default scoring
        if not hasattr(model, 'scoring'):
            logger.info('older version of %s loaded without scoring function', cls.__name__)
            logger.info('setting pluggable scoring method to original_scorer for compatibility')
            model.scoring = original_scorer
        # if there is a scoring parameter, and it's a text value, load the proper scoring function
        if hasattr(model, 'scoring'):
            if isinstance(model.scoring, six.string_types):
                if model.scoring == 'default':
                    logger.info('older version of %s loaded with "default" scoring parameter', cls.__name__)
                    logger.info('setting scoring method to original_scorer pluggable scoring method for compatibility')
                    model.scoring = original_scorer
                elif model.scoring == 'npmi':
                    logger.info('older version of %s loaded with "npmi" scoring parameter', cls.__name__)
                    logger.info('setting scoring method to npmi_scorer pluggable scoring method for compatibility')
                    model.scoring = npmi_scorer
                else:
                    raise ValueError(
                        'failed to load %s model with unknown scoring setting %s' % (cls.__name__, model.scoring))
        # if there is non common_terms attribute, initialize
        if not hasattr(model, "common_terms"):
            logger.info('older version of %s loaded without common_terms attribute', cls.__name__)
            logger.info('setting common_terms to empty set')
            model.common_terms = frozenset()
        return model


class Phrases(SentenceAnalyzer, PhrasesTransformation):
    """Detect phrases, based on collected collocation counts. """

    def __init__(self, sentences=None, min_count=5, threshold=10.0,
                 max_vocab_size=40000000, delimiter=b'_', progress_per=10000,
                 scoring='default', common_terms=frozenset()):
        """
        sentences : list of str, optional
            The `sentences` iterable can be simply a list, but for larger corpora,
            consider a generator that streams the sentences directly from disk/network,
            without storing everything in RAM. See :class:`~gensim.models.word2vec.BrownCorpus`,
            :class:`~gensim.models.word2vec.Text8Corpus` or :class:`~gensim.models.word2vec.LineSentence`
            in the :mod:`~gensim.models.word2vec` module for such examples.
        min_count : int, optional
            Ignore all words and bigrams with total collected count lower
            than this.
        threshold : int, optional
            Represent a score threshold for forming the phrases (higher means fewer phrases).
            A phrase of words `a` followed by `b` is accepted if the score of the
            phrase is greater than threshold. See the `scoring` setting.
        max_vocab_size : int, optional
            Maximum size of the vocabulary. Used to control pruning of less common words, to keep memory under control.
            The default of 40M needs about 3.6GB of RAM; increase/decrease `max_vocab_size` depending on how much
            available memory you have.
        delimiter : str, optional
            Glue character used to join collocation tokens, should be a byte string (e.g. b'_').
        scoring : {'default', 'npmi'}, optional
            Specify how potential phrases are scored for comparison to the `threshold` setting.
            `scoring` can be set with either a string that refers to a built-in scoring function, or with a function
            with the expected parameter names. Two built-in scoring functions are available by setting `scoring` to a
            string:

            1. `default` - :meth:`~gensim.models.phrases.original_scorer`.
            2. `npmi` - :meth:`~gensim.models.phrases.npmi_scorer`.

        common_terms : set of str, optional
            List of "stop words" that won't affect frequency count of expressions containing them.

        Notes
        -----
        'npmi' is more robust when dealing with common words that form part of common bigrams, and
        ranges from -1 to 1, but is slower to calculate than the default.

        To use a custom scoring function, create a function with the following parameters and set the `scoring`
        parameter to the custom function. You must use all the parameters in your function call, even if the
        function does not require all the parameters.

            worda_count: number of occurrances in `sentences` of the first token in the phrase being scored
            wordb_count: number of occurrances in `sentences` of the second token in the phrase being scored
            bigram_count: number of occurrances in `sentences` of the phrase being scored
            len_vocab: the number of unique tokens in `sentences`
            min_count: the `min_count` setting of the Phrases class
            corpus_word_count: the total number of (non-unique) tokens in `sentences`

        A scoring function without any of these parameters (even if the parameters are not used) will
        raise a ValueError on initialization of the Phrases class. The scoring function must be picklable.

        Initialize the model from an iterable of `sentences`. Each sentence must be
        a list of words (unicode strings) that will be used for training.

        Adjacent words that appear together more frequently than
        expected are joined together with the `_` character. It can be used to generate phrases on the fly,
        using the `phrases[sentence]` and `phrases[corpus]` syntax.

        Example
        ----------
        >>> from gensim.test.utils import datapath
        >>> from gensim.models.word2vec import Text8Corpus
        >>> from gensim.models.phrases import Phrases
        >>> sentences = Text8Corpus(datapath('testcorpus.txt'))
        >>> bigram = Phrases(sentences, min_count=5, threshold=100)
        >>> print bigram
        Phrases<37 vocab, min_count=5, threshold=100, max_vocab_size=40000000>

        """
        if min_count <= 0:
            raise ValueError("min_count should be at least 1")

        if threshold <= 0 and scoring == 'default':
            raise ValueError("threshold should be positive for default scoring")
        if scoring == 'npmi' and (threshold < -1 or threshold > 1):
            raise ValueError("threshold should be between -1 and 1 for npmi scoring")

        # set scoring based on string
        # intentially override the value of the scoring parameter rather than set self.scoring here,
        # to still run the check of scoring function parameters in the next code block

        if isinstance(scoring, six.string_types):
            if scoring == 'default':
                scoring = original_scorer
            elif scoring == 'npmi':
                scoring = npmi_scorer
            else:
                raise ValueError('unknown scoring method string %s specified' % (scoring))

        scoring_parameters = [
            'worda_count', 'wordb_count', 'bigram_count', 'len_vocab', 'min_count', 'corpus_word_count'
        ]
        if callable(scoring):
            if all(parameter in getargspec(scoring)[0] for parameter in scoring_parameters):
                self.scoring = scoring
            else:
                raise ValueError('scoring function missing expected parameters')

        self.min_count = min_count
        self.threshold = threshold
        self.max_vocab_size = max_vocab_size
        self.vocab = defaultdict(int)  # mapping between utf8 token => its count
        self.min_reduce = 1  # ignore any tokens with count smaller than this
        self.delimiter = delimiter
        self.progress_per = progress_per
        self.corpus_word_count = 0
        self.common_terms = frozenset(utils.any2utf8(w) for w in common_terms)

        # ensure picklability of custom scorer
        try:
            test_pickle = pickle.dumps(self.scoring)
            load_pickle = pickle.loads(test_pickle)
        except pickle.PickleError:
            raise pickle.PickleError('unable to pickle custom Phrases scoring function')
        finally:
            del(test_pickle)
            del(load_pickle)

        if sentences is not None:
            self.add_vocab(sentences)

    @classmethod
    def load(cls, *args, **kwargs):
        """Load a previously saved Phrases class. Handles backwards compatibility from older Phrases versions
        which did not support pluggable scoring functions.

        Parameters
        ----------
        args : object
            Sequence of arguments, see :class:`~gensim.models.phrases.Phrases` for more information.
        kwargs : object
            Sequence of arguments, see :class:`~gensim.models.phrases.Phrases` for more information.

        """
        model = super(Phrases, cls).load(*args, **kwargs)
        if not hasattr(model, 'corpus_word_count'):
            logger.info('older version of %s loaded without corpus_word_count', cls.__name__)
            logger.info('Setting it to 0, do not use it in your scoring function.')
            model.corpus_word_count = 0
        return model

    def __str__(self):
        """Get short string representation of this phrase detector."""
        return "%s<%i vocab, min_count=%s, threshold=%s, max_vocab_size=%s>" % (
            self.__class__.__name__, len(self.vocab), self.min_count,
            self.threshold, self.max_vocab_size
        )

    @staticmethod
    def learn_vocab(sentences, max_vocab_size, delimiter=b'_', progress_per=10000,
                    common_terms=frozenset()):
        """Collect unigram/bigram counts from the `sentences` iterable.

        Parameters
        ----------
        sentences : iterable of list of str
        max_vocab_size : int
            Maximal vocabulary size.
        delimiter : str
            Define, what will be used for string split.
        progress_per : int
            Write logs every `progress_per` milliseconds.
        common_terms : set of str
            Set of common words.

        Return
        ------
        int, dict, int
            Minimal frequency threshold for tokens in vocabulary of word types, vocabulary of word types,
            total number of words.

        Example
        ----------
        >>> from gensim.test.utils import datapath
        >>> from gensim.models.word2vec import Text8Corpus
        >>> from gensim.models.phrases import Phrases
        >>> sentences = Text8Corpus(datapath('testcorpus.txt'))
        >>> learned = Phrases.learn_vocab(sentences,40000)
        >>> print learned
        (1, defaultdict(<type 'int'>, {...}), 29)

        """
        sentence_no = -1
        total_words = 0
        logger.info("collecting all words and their counts")
        vocab = defaultdict(int)
        min_reduce = 1
        for sentence_no, sentence in enumerate(sentences):
            if sentence_no % progress_per == 0:
                logger.info(
                    "PROGRESS: at sentence #%i, processed %i words and %i word types",
                    sentence_no, total_words, len(vocab),
                )
            s = [utils.any2utf8(w) for w in sentence]
            last_uncommon = None
            in_between = []
            for word in s:
                if word not in common_terms:
                    vocab[word] += 1
                    if last_uncommon is not None:
                        components = it.chain([last_uncommon], in_between, [word])
                        vocab[delimiter.join(components)] += 1
                    last_uncommon = word
                    in_between = []
                elif last_uncommon is not None:
                    in_between.append(word)
                total_words += 1

            if len(vocab) > max_vocab_size:
                utils.prune_vocab(vocab, min_reduce)
                min_reduce += 1

        logger.info(
            "collected %i word types from a corpus of %i words (unigram + bigrams) and %i sentences",
            len(vocab), total_words, sentence_no + 1
        )
        return min_reduce, vocab, total_words

    def add_vocab(self, sentences):
        """Merge the collected counts `vocab` into this phrase detector.

        Parameters
        ----------
        sentences : list of str
            List of unicode strings.

        Example
        -------
        >>> from gensim.test.utils import datapath
        >>> from gensim.models.word2vec import Text8Corpus
        >>> from gensim.models.phrases import Phrases
        >>> sentences = Text8Corpus(datapath('testcorpus.txt'))
        >>> bigram = Phrases(sentences, min_count=5, threshold=100)

        """
        # uses a separate vocab to collect the token counts from `sentences`.
        # this consumes more RAM than merging new sentences into `self.vocab`
        # directly, but gives the new sentences a fighting chance to collect
        # sufficient counts, before being pruned out by the (large) accummulated
        # counts collected in previous learn_vocab runs.
        min_reduce, vocab, total_words = self.learn_vocab(
            sentences, self.max_vocab_size, self.delimiter, self.progress_per, self.common_terms)

        self.corpus_word_count += total_words
        if len(self.vocab) > 0:
            logger.info("merging %i counts into %s", len(vocab), self)
            self.min_reduce = max(self.min_reduce, min_reduce)
            for word, count in iteritems(vocab):
                self.vocab[word] += count
            if len(self.vocab) > self.max_vocab_size:
                utils.prune_vocab(self.vocab, self.min_reduce)
                self.min_reduce += 1
            logger.info("merged %s", self)
        else:
            # in common case, avoid doubling gigantic dict
            logger.info("using %i counts as vocab in %s", len(vocab), self)
            self.vocab = vocab

    def export_phrases(self, sentences, out_delimiter=b' ', as_tuples=False):
        """Generate an iterator that contains all phrases in given 'sentences'

        Parameters
        ----------
        sentences : list of str
            List of unicode strings.
        out_delimiter : str, optional
            Define, what will be used for string split.
        as_tuples : bool, optional
            If true, yield (tuple(words), score), otherwise - (out_delimiter.join(words), score).


        Example
        -------
        >>> from gensim.test.utils import datapath
        >>> from gensim.models.word2vec import Text8Corpus
        >>> from gensim.models.phrases import Phrases
        >>> sentences = Text8Corpus(datapath('testcorpus.txt'))
        >>> bigram = Phrases(sentences, min_count=5, threshold=100)
        >>> for phrase, score in bigram.export_phrases(sentences):
        ...     print(u'{0}\t{1}'.format(phrase, score))
            then you can debug the threshold with generated tsv

        """
        analyze_sentence = ft.partial(
            self.analyze_sentence,
            threshold=self.threshold,
            common_terms=self.common_terms,
            scorer=ft.partial(
                self.scoring,
                len_vocab=float(len(self.vocab)),
                min_count=float(self.min_count),
                corpus_word_count=float(self.corpus_word_count),
            ),
        )
        for sentence in sentences:
            bigrams = analyze_sentence(sentence)
            # keeps only not None scores
            filtered = ((words, score) for words, score in bigrams if score is not None)
            for words, score in filtered:
                if as_tuples:
                    yield (tuple(words), score)
                else:
                    yield (out_delimiter.join(words), score)

    def __getitem__(self, sentence):
        """Convert the input tokens `sentence` (=list of unicode strings) into phrase
        tokens (=list of unicode strings, where detected phrases are joined by u'_').

        If `sentence` is an entire corpus (iterable of sentences rather than a single
        sentence), return an iterable that converts each of the corpus' sentences
        into phrases on the fly, one after another.

        Parameters
        ----------
        sentence : list of str
            List of unicode strings.

        Example
        ----------
        >>> from gensim.test.utils import datapath
        >>> from gensim.models.word2vec import Text8Corpus
        >>> from gensim.models.phrases import Phrases
        >>> sentences = Text8Corpus(datapath('testcorpus.txt'))
        >>> bigram = Phrases(sentences, min_count=5, threshold=100)
        >>> bigram["trees","grass"]
        [u'trees', u'grass']

        """
        warnings.warn("For a faster implementation, use the gensim.models.phrases.Phraser class")

        delimiter = self.delimiter  # delimiter used for lookup

        is_single, sentence = _is_single(sentence)
        if not is_single:
            # if the input is an entire corpus (rather than a single sentence),
            # return an iterable stream.
            return self._apply(sentence)

        delimiter = self.delimiter
        bigrams = self.analyze_sentence(
            sentence,
            threshold=self.threshold,
            common_terms=self.common_terms,
            scorer=ft.partial(
                self.scoring,
                len_vocab=float(len(self.vocab)),
                min_count=float(self.min_count),
                corpus_word_count=float(self.corpus_word_count),
            ),
        )
        new_s = []
        for words, score in bigrams:
            if score is not None:
                words = delimiter.join(words)
            new_s.append(words)

        return [utils.to_unicode(w) for w in new_s]


# these two built-in scoring methods don't cast everything to float because the casting is done in the call
# to the scoring method in __getitem__ and export_phrases.

# calculation of score based on original mikolov word2vec paper
def original_scorer(worda_count, wordb_count, bigram_count, len_vocab, min_count, corpus_word_count):
    """
    Parameters
    ----------
    worda_count : int
        First word for comparison.
    wordb : str
        Second word for comparison.
    components : TODO
    scorer : {'default', 'npmi'}
        Scorer function, as given to :class:`~gensim.models.phrases.Phrases`.

    Notes
    -----
    from "Efficient Estimaton of Word Representations in Vector Space" by
    Mikolov, et. al.:
    :math:`(count(worda, wordb) - min_count) * N /
    (count(worda) * count(wordb)) > threshold`, where `N` is the total vocabulary size.

    """
    return (bigram_count - min_count) / worda_count / wordb_count * len_vocab


# normalized PMI, requires corpus size
def npmi_scorer(worda_count, wordb_count, bigram_count, len_vocab, min_count, corpus_word_count):
    """

    Parameters
    ----------
    worda_count: int
    wordb_count: int
    bigram_count: int
    len_vocab: int
    min_count: int
    corpus_word_count: int

    Notes
    -----
    Normalized pointwise mutual information, from "Normalized (Pointwise) Mutual
    Information in Colocation Extraction" by Gerlof Bouma:
    :math:`ln(prop(worda, wordb) / (prop(worda)*prop(wordb))) / - ln(prop(worda, wordb)`
    where prop(n) is the count of n / the count of everything in the entire corpus.

    """
    pa = worda_count / corpus_word_count
    pb = wordb_count / corpus_word_count
    pab = bigram_count / corpus_word_count
    return log(pab / (pa * pb)) / -log(pab)


def pseudocorpus(source_vocab, sep, common_terms=frozenset()):
    """Feeds source_vocab's compound keys back to it, to discover phrases.

    Parameters
    ----------
    source_vocab : iterable of list of str
        Vocabulary.
    sep : str
        Separator.
    common_terms : set, optional
        Stopwords.

    Yields
    ------
    generator
        Generator with phrases.

    """
    for k in source_vocab:
        if sep not in k:
            continue
        unigrams = k.split(sep)
        for i in range(1, len(unigrams)):
            if unigrams[i - 1] not in common_terms:
                # do not join common terms
                cterms = list(it.takewhile(lambda w: w in common_terms, unigrams[i:]))
                tail = unigrams[i + len(cterms):]
                components = [sep.join(unigrams[:i])] + cterms
                if tail:
                    components.append(sep.join(tail))
                yield components


class Phraser(SentenceAnalyzer, PhrasesTransformation):
    """Minimal state & functionality to apply results of a Phrases model to tokens.

    Notes
    -----
    After the one-time initialization, a Phraser will be much smaller and
    somewhat faster than using the full Phrases model.

    Reflects the results of the source model's `min_count`, `threshold`, and
    `scoring` settings. (You can tamper with those & create a new Phraser to try
    other values.)

    """

    def __init__(self, phrases_model):
        """
        Parameters
        ----------
        phrases_model : :class:`~gensim.models.phrases.Phrases`
            Phrases class object.

        Example
        -------
        >>> from gensim.test.utils import datapath
        >>> from gensim.models.word2vec import Text8Corpus
        >>> from gensim.models.phrases import Phrases, Phraser
        >>> sentences = Text8Corpus(datapath('testcorpus.txt'))
        >>> phrases_model = Phrases(sentences, min_count=5, threshold=100)
        >>> phraser_model = Phraser(phrases_model)

        """
        self.threshold = phrases_model.threshold
        self.min_count = phrases_model.min_count
        self.delimiter = phrases_model.delimiter
        self.scoring = phrases_model.scoring
        self.common_terms = phrases_model.common_terms
        corpus = self.pseudocorpus(phrases_model)
        self.phrasegrams = {}
        logger.info('source_vocab length %i', len(phrases_model.vocab))
        count = 0
        for bigram, score in phrases_model.export_phrases(corpus, self.delimiter, as_tuples=True):
            if bigram in self.phrasegrams:
                logger.info('Phraser repeat %s', bigram)
            self.phrasegrams[bigram] = (phrases_model.vocab[self.delimiter.join(bigram)], score)
            count += 1
            if not count % 50000:
                logger.info('Phraser added %i phrasegrams', count)
        logger.info('Phraser built with %i %i phrasegrams', count, len(self.phrasegrams))

    def pseudocorpus(self, phrases_model):
        """Phrase searcher. #TODO: please check it

        Parameters
        ----------
        phrases_model : :class:`~gensim.models.phrases.Phrases`
            Phrases class object.

        Return
        ------
        generator
            Generator with phrases.


        Example
        -------
        >>> from gensim.test.utils import datapath
        >>> from gensim.models.word2vec import Text8Corpus
        >>> from gensim.models.phrases import Phrases, Phraser
        >>> sentences = Text8Corpus(datapath('testcorpus.txt'))
        >>> phrases_model = Phrases(sentences, min_count=5, threshold=100)
        >>> phraser_model = Phraser(phrases_model)
        >>> pseudo = phraser_model.pseudocorpus(phrases_model)

        """
        return pseudocorpus(phrases_model.vocab, phrases_model.delimiter,
                            phrases_model.common_terms)

    def score_item(self, worda, wordb, components, scorer):
        """Score, retained from original dataset.

        Parameters
        ----------
        worda : str
            First word for comparison. Should be unicode string.
        wordb : str
            Second word for comparison. Should be unicode string.
        components : generator
        scorer : {'default', 'npmi'}
            Scorer function, as given to :class:`~gensim.models.phrases.Phrases`.

        Return
        ------
        dict
            Phrasegrams.

        Example
        -------
        >>> from gensim.test.utils import datapath
        >>> from gensim.models.word2vec import Text8Corpus
        >>> from gensim.models.phrases import Phrases, Phraser
        >>> sentences = Text8Corpus(datapath('testcorpus.txt'))
        >>> phrases_model = Phrases(sentences, min_count=5, threshold=100)
        >>> phraser_model = Phraser(phrases_model)
        >>> pseudo = phraser_model.pseudocorpus(phrases_model)
        //>>> phraser_model.score_item("tree","human",pseudo,'default')
        >>> phraser_model.score_item(u"tree",u"human",pseudo,'default')
        -1

        """
        try:
            return self.phrasegrams[tuple(components)][1]
        except KeyError:
            return -1

    def __getitem__(self, sentence):
        """Convert the input tokens `sentence` into phrase
        tokens .

        Parameters
        ----------
        sentence : {list of str, iterable of list of str}
            Sentence tokens - list of unicode strings.

        Return
        ------
        {list of str, iterable of list of str}
            Phrase tokens, where joined by delimiter-character.

        Notes
        -----
        If `sentence` is an entire corpus (iterable of sentences rather than a single
        sentence), return an iterable that converts each of the corpus' sentences
        into phrases on the fly, one after another.

        Example
        -------
        >>> from gensim.test.utils import datapath
        >>> from gensim.models.word2vec import Text8Corpus
        >>> from gensim.models.phrases import Phrases, Phraser
        >>> sentences = Text8Corpus(datapath('testcorpus.txt'))
        >>> phrases_model = Phrases(sentences, min_count=5, threshold=100)
        >>> phraser_model = Phraser(phrases_model)
        >>> pseudo = phraser_model.pseudocorpus(phrases_model)
        >>> phraser_model["tree", "human"]
        [u'tree', u'human']


        """
        is_single, sentence = _is_single(sentence)
        if not is_single:
            # if the input is an entire corpus (rather than a single sentence),
            # return an iterable stream.
            return self._apply(sentence)

        delimiter = self.delimiter
        bigrams = self.analyze_sentence(
            sentence,
            threshold=self.threshold,
            common_terms=self.common_terms,
            scorer=None)  # we will use our score_item function redefinition
        new_s = []
        for words, score in bigrams:
            if score is not None:
                words = delimiter.join(words)
            new_s.append(words)
        return [utils.to_unicode(w) for w in new_s]


if __name__ == '__main__':
    logging.basicConfig(format='%(asctime)s : %(threadName)s : %(levelname)s : %(message)s', level=logging.INFO)
    logging.info("running %s", " ".join(sys.argv))

    # check and process cmdline input
    program = os.path.basename(sys.argv[0])
    if len(sys.argv) < 2:
        print(globals()['__doc__'] % locals())
        sys.exit(1)
    infile = sys.argv[1]

    from gensim.models import Phrases  # noqa:F811 for pickle
    from gensim.models.word2vec import Text8Corpus
    sentences = Text8Corpus(infile)

    # test_doc = LineSentence('test/test_data/testcorpus.txt')
    bigram = Phrases(sentences, min_count=5, threshold=100)
    for s in bigram[sentences]:
        print(utils.to_utf8(u' '.join(s)))
