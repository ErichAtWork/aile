import sys
import heapq
import codecs

import pandas as pd
import numpy as np
import scipy.spatial.distance as dst
import scrapely.htmlpage as hp

from phmm import ProfileHMM
import util

def phmm_cmp(W, Z1, Z2):
    return ((Z1 >= W) != (Z2 >= W)).mean()


def demo1():
    phmm_true = ProfileHMM(
        f=np.array([
            [0.2, 0.3, 0.2, 0.3],
            [0.9, 0.1, 0.0, 0.0],
            [0.2, 0.8, 0.0, 0.0],
            [0.0, 0.0, 0.8, 0.2]]),
        t = np.array([
            0.05, 0.9, 0.1, 0.05, 0.85, 0.1])
    )

    X, Z = phmm_true.generate(5000)
    phmm = ProfileHMM.fit(X, 3)

    print "True model 't' parameters", phmm_true.t
    print " Estimated 't' paramaters", phmm.t

    z, logP = phmm.viterbi(X)
    print 'Error finding motifs (% mismatch):', phmm_cmp(phmm.W, Z, z)*100


def html_guess_emissions(code_book, W, X, n=1):
    """Given a sequence X, the code_book used to encode it and the motif
    width W, guess the initial value of the emission matrix"""
    s = code_book.code('/>') # integer for the closing tag symbol
    emissions = []
    priors = []
    # candidates start with a non-closing tag and end with a closing one
    candidates = [i for i in range(len(X) - W) if X[i] != s and X[i + W] == s]
    for i in util.random_pick(candidates, n):
        f = util.guess_emissions(code_book, X[i:i+W])
        f[1, s] = 0.0 # zero probability of starting motif with closing tag
        f[W, :] = 0.0 # zero probability of ending motif with non-closing tag
        f[W, s] = 1.0 # probability 1 of ending motif with closing tag
        emissions.append(util.normalized(f))
        eps = f[0, :].repeat(W).reshape(f[1:,:].shape)
        eps[  0, s] = 1e-6
        eps[W-1, :] = 1e-6
        eps[W-1, s] = 1.0
        priors.append(1.0 + 1e-3*util.normalized(eps))
    return emissions, priors


def tagify(page):
    def convert(fragment):
        if (fragment.is_text_content and
            page.body[fragment.start:fragment.end].strip()):
            yield ('[T]', fragment)
        elif isinstance(fragment, hp.HtmlTag):
            if fragment.tag_type != hp.HtmlTagType.CLOSE_TAG:
                tag_class = fragment.attributes.get('class', None)
                if tag_class:
                    yield (fragment.tag, fragment)
                    yield (tag_class, None)
                else:
                    yield (fragment.tag, fragment)
            else:
                yield ('/>', fragment)
        else:
            yield (None, None)
    return filter(lambda x: x[0] is not None,
                  [x for f in page.parsed_body for x in convert(f)])


def match_tags(tags):
    match = np.repeat(-1, len(tags))
    stack = []
    for i, tag in enumerate(tags):
        if isinstance(tag, hp.HtmlTag):
            if tag.tag_type == hp.HtmlTagType.OPEN_TAG:
                stack.append((i, tag))
            elif (tag.tag_type == hp.HtmlTagType.CLOSE_TAG and
                  stack):
                last_i, last_tag = stack[-1]
                if (last_tag.tag_type == hp.HtmlTagType.OPEN_TAG and
                    last_tag.tag == tag.tag):
                    match[last_i] = i
                    stack.pop()
    return match


def extract_motifs(phmm, tags, fragments, m=0.2, G=0.3):
    X = np.array(map(phmm.code_book.code, tags))
    Z, logP = phmm.viterbi(X)
    match = match_tags(fragments)
    i = 0
    while i < len(match):
        j = match[i]
        if j > 0:
            k = j
            while k - i <= phmm.W*(1.0 + m):
                if k - i >= phmm.W*(1.0 - m):
                    H = np.sum(
                            np.abs(np.bincount(Z[i:k], minlength=phmm.S)[phmm.W:] - 1)
                        )/float(phmm.W)
                    if H <= G:
                        score = phmm.score(X[i:k], Z[i:k])/(k - i)
                        yield (i, k), Z[i:k], score, H
                        i = k
                        break
                k = match[k + 1]
                if k < j:
                    break
        i += 1


def adjust(phmm, matches):
    r = np.zeros((phmm.W, ), dtype=int)
    for (i, j), Z, score, H in matches:
        for k, z in enumerate(Z):
            if z >= phmm.W:
                r[z - phmm.W] += 1
                break
    start = np.argmax(r)
    phmm2 = ProfileHMM(
        f   = np.vstack((
                phmm.f[ 0, :],
                np.roll(phmm.f[1:phmm.W + 1 , :], -start, axis=0),
                np.roll(phmm.f[  phmm.W + 1:, :], -start, axis=0))),
        t   = phmm.t,
        eps = phmm.eps,
        p0  = phmm.p0)
    phmm2.code_book = phmm.code_book
    return phmm2


def itemize(phmm, ratio=2.0, tags=['[T]', 'a', 'img']):
    a = map(phmm.code_book.code, tags)
    h = phmm.f[0,a]
    return [phmm.W + j for j, g in enumerate(phmm.f[1:])
            if np.any(g[a]/h >= ratio)]


def guess_motif_width(X, n_estimates=2, min_width=10, max_width=400):
    D = [(dst.hamming(X[:-w], X[w:]), w)
         for w in np.arange(min_width, max_width)]
    return [min_width + w
            for d, w in heapq.nsmallest(n_estimates, D, key=lambda x: x[0])]


def extract_items(page, fragments, motifs, fields):
    items = []
    for (i, j), Z, score, H in motifs:
        f = {}
        for k, z in enumerate(Z):
            if z in fields:
                fragment = fragments[i + k]
                if fragment is not None:
                    if fragment.is_text_content:
                        f[z] = page.body[fragment.start:fragment.end]
                    elif (isinstance(fragment, hp.HtmlTag) and
                          fragment.tag_type != hp.HtmlTagType.CLOSE_TAG):
                        if fragment.tag == 'a':
                            f[z] = fragment.attributes.get('href', None)
                        if fragment.tag == 'img':
                            f[z] = fragment.attributes.get('src', None)
        items.append([f.get(field, None) for field in fields])
    return pd.DataFrame.from_records(items)


def uninformative_fields(items):
    return [col
            for col in items.columns
            if (items[col][0] == items[col]).all()]


def train_test(pattern, start, end):
    return ([pattern.format(i) for i in range(start, end + 1)],
            pattern.format(end + 1))


def train_test_1(n_train=2):
    return train_test('https://news.ycombinator.com/news?p={0}', 1, n_train + 1)


def train_test_2(n_train=12):
    return train_test('https://patchofland.com/investments/page/{0}.html', 1, n_train + 1)


def train_test_3(n_train=6):
    return train_test('http://www.ebay.com/sch/Tires-/66471/i.html?_pgn={0}', 1, n_train + 1)


def train_test_4(n_train=6):
    return train_test('http://jobsearch.monster.co.uk/browse/?pg={0}&re=nv_gh_gnl1147_%2F', 1, n_train + 1)


def train_test_5(n_train=3):
    pattern = 'http://lambda-the-ultimate.org/node?from={0}'
    return ([pattern.format(i) for i in range(0, n_train*10, 10)],
            pattern.format(n_train*10))


def train_test_6(n_train=3):
    return train_test('http://arstechnica.com/page/{0}/', 1, n_train + 1)


def demo2(train_test, out='demo.html'):
    train_urls, test_url = train_test

    print 'Downloading and parsing test urls... ',
    tags_1, fragments_1 = zip(*[
        (tag, fragment)
        for url in train_urls
        for tag, fragment in tagify(hp.url_to_page(url))
    ])
    print 'done'
    X_train = np.array(tags_1)
    W = guess_motif_width(X_train, n_estimates=2)
    print 'Motif width guess:', W
    phmm = ProfileHMM.fit(
        X_train,
        W,
        guess_emissions=html_guess_emissions,
        precision=1e-3)
    print 'Motif width      :', phmm.W
    train_motifs = extract_motifs(phmm, tags_1, fragments_1)
    phmm = adjust(phmm, train_motifs)
    fields = itemize(phmm)

    page = hp.url_to_page(test_url)
    tags_2, fragments_2 = zip(*tagify(page))

    motifs = extract_motifs(phmm, tags_2, fragments_2)
    items = extract_items(page, fragments_2, motifs, fields)

    outf = codecs.open(out, 'w', encoding='utf-8')
    items.to_html(outf)
    print items

    return phmm


if __name__ == '__main__':
    tests = [
        train_test_1,
        train_test_2,
        train_test_3,
        train_test_4,
        train_test_5,
        train_test_6
    ]

    phmm = demo2(tests[int(sys.argv[1])-1]())
