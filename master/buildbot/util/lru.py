# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

from weakref import WeakValueDictionary
from itertools import ifilterfalse
from twisted.python import log
from twisted.internet import defer
from collections import deque
from buildbot.util.bbcollections import defaultdict


class LRUCache(object):
    """
    A least-recently-used cache, with a fixed maximum size.  This cache is
    designed to control memory usage by minimizing duplication of objects,
    while avoiding unnecessary re-fetching of the same rows from the database.

    All values are also stored in a weak valued dictionary, even after they
    have expired from the cache.  This allows values that are used elsewhere in
    Buildbot to "stick" in the cache in case they are needed by another
    component.  Weak references cannot be used for some types, so these types
    are not compatible with this class.  Note that dictionaries can be weakly
    referenced if they are an instance of a subclass of C{dict}.

    If the result of the C{miss_fn} is C{None}, then the value is not cached;
    this is intended to avoid caching negative results.

    This is based on Raymond Hettinger's implementation in
    U{http://code.activestate.com/recipes/498245-lru-and-lfu-cache-decorators/}
    licensed under the PSF license, which is GPL-compatiblie.

    @ivar hits: cache hits so far
    @ivar refhits: cache misses found in the weak ref dictionary, so far
    @ivar misses: cache misses leading to re-fetches, so far
    @ivar max_size: maximum allowed size of the cache
    """

    sentinel = object()
    QUEUE_SIZE_FACTOR = 10

    def __init__(self, miss_fn, max_size=50):
        """
        Constructor.

        @param miss_fn: function to call, with key as parameter, for cache
        misses.

        @param max_size: maximum number of objects in the cache
        """
        self.max_size = max_size
        self.max_queue = max_size * self.QUEUE_SIZE_FACTOR
        self.queue = deque()
        self.cache = {}
        self.weakrefs = WeakValueDictionary()
        self.hits = self.misses = self.refhits = 0
        self.refcount = defaultdict(lambda : 0)
        self.miss_fn = miss_fn

    def put(self, key, value):
        if key in self.cache:
            self.cache[key] = value
            self.weakrefs[key] = value
        elif key in self.weakrefs:
            self.weakrefs[key] = value

    def get(self, key, **miss_fn_kwargs):
        """
        Fetch a value from the cache by key, invoking C{self.miss_fn(key)}
        if the key is not in the cache.

        Any additional keyword arguments are passed to the C{miss_fn} as
        keyword arguments; these can supply additional information relating to
        the key.  It is up to the caller to ensure that this information is
        functionally identical for each key value: if the key is already in the
        cache, the C{miss_fn} will not be invoked, even if the keyword
        arguments differ.

        @param key: cache key
        @param **miss_fn_kwargs: keyword arguments to  the miss_fn
        @returns: cached value
        """
        try:
            result = self._get_hit(key)
            return result
        except KeyError:
            pass

        self.misses += 1

        result = self.miss_fn(key, **miss_fn_kwargs)
        if result is not None:
            self.cache[key] = result
            self.weakrefs[key] = result
            self._ref_key(key)
            self._purge()

        return result

    def set_max_size(self, max_size):
        if self.max_size == max_size:
            return

        self.max_size = max_size
        self.max_queue = max_size * self.QUEUE_SIZE_FACTOR
        self._purge()

    def inv(self):
        global inv_failed

        # the keys of the queue and cache should be identical
        cache_keys = set(self.cache.keys())
        queue_keys = set(self.queue)
        if queue_keys - cache_keys:
            log.msg("INV: uncached keys in queue:", queue_keys - cache_keys)
            inv_failed = True
        if cache_keys - queue_keys:
            log.msg("INV: unqueued keys in cache:", cache_keys - queue_keys)
            inv_failed = True

        # refcount should always represent the number of times each key appears
        # in the queue
        exp_refcount = dict()
        for k in self.queue:
            exp_refcount[k] = exp_refcount.get(k, 0) + 1
        if exp_refcount != self.refcount:
            log.msg("INV: refcounts differ:")
            log.msg(" expected:", sorted(exp_refcount.items()))
            log.msg("      got:", sorted(self.refcount.items()))
            inv_failed = True

    def _ref_key(self, key):
        queue = self.queue
        refcount = self.refcount

        queue.append(key)
        refcount[key] = refcount[key] + 1

        # periodically compact the queue by eliminating duplicate keys
        # while preserving order of most recent access.  Note that this
        # is only required when the cache does not exceed its maximum
        # size
        if len(queue) > self.max_queue:
            refcount.clear()
            queue_appendleft = queue.appendleft
            queue_appendleft(self.sentinel)
            for k in ifilterfalse(refcount.__contains__,
                                    iter(queue.pop, self.sentinel)):
                queue_appendleft(k)
                refcount[k] = 1

    def _get_hit(self, key):
        try:
            result = self.cache[key]
            self.hits += 1
            self._ref_key(key)
            return result
        except KeyError:
            pass

        result = self.weakrefs[key]
        self.refhits += 1
        self.cache[key] = result
        self._ref_key(key)
        return result

    def _purge(self):
        if len(self.cache) <= self.max_size:
            return

        cache = self.cache
        refcount = self.refcount
        queue = self.queue
        max_size = self.max_size

        # purge least recently used entries, using refcount to count entries
        # that appear multiple times in the queue
        while len(cache) > max_size:
            refc = 1
            while refc:
                k = queue.popleft()
                refc = refcount[k] = refcount[k] - 1
            del cache[k]
            del refcount[k]


class AsyncLRUCache(LRUCache):
    """
    An LRU cache with asynchronous locking to ensure that in the common case of
    multiple concurrent requests for the same key, only one fetch is performed.
    """

    def __init__(self, miss_fn, max_size=50):
        """
        Constructor.

        @param miss_fn: function to call, with key as parameter, for cache
        misses.  This function I{must} return a deferred.

        @param max_size: maximum number of objects in the cache
        """
        super(AsyncLRUCache, self).__init__(miss_fn, max_size=max_size)
        self.concurrent = {}

    def get(self, key, **miss_fn_kwargs):
        """
        Overrides LRUCache.get.

        @param key: cache key
        @param **miss_fn_kwargs: keyword arguments to  the miss_fn
        @returns: cached value via Deferred
        """
        try:
            result = self._get_hit(key)
            return defer.succeed(result)
        except KeyError:
            pass

        concurrent = self.concurrent
        conc = concurrent.get(key)
        if conc:
            self.hits += 1
            d = defer.Deferred()
            conc.append(d)
            return d

        # if we're here, we've missed and need to fetch
        self.misses += 1

        # create a list of waiting deferreds for this key
        d = defer.Deferred()
        assert key not in concurrent
        concurrent[key] = [ d ]

        miss_d = self.miss_fn(key, **miss_fn_kwargs)

        def handle_result(result):
            if result is not None:
                self.cache[key] = result
                self.weakrefs[key] = result

                # reference the key once, possibly standing in for multiple
                # concurrent accesses
                self._ref_key(key)

                self._purge()

            # and fire all of the waiting Deferreds
            dlist = concurrent.pop(key)
            for d in dlist:
                d.callback(result)

        def handle_failure(f):
            # errback all of the waiting Deferreds
            dlist = concurrent.pop(key)
            for d in dlist:
                d.errback(f)

        miss_d.addCallbacks(handle_result, handle_failure)
        miss_d.addErrback(log.err)

        return d


# for tests
inv_failed = False
