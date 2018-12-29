'''module for replacing webcache._open_client function and Pylibmc Client with
a mocked-out one

Incomplete implementation; only mocks out features currently in use

Not thread-safe'''

import pylibmc
import logging

logger = logging.getLogger(__name__)

class MockEntry(object):
	def __init__(self, key, value, time, client):
		self.client = client
		self.timestamp = client.time()

		self.key = key
		self.value = value
		self.time = time

		self.etag = id(self)

	@property
	def expired(self):
		if self.time is not None:
			return self.client.time() > (self.timestamp + self.time)
		return False

class MockPylibmcClient(object):
	CONTEST = object()

	def __init__(self, timesource):
		self.__store = {}
		self.__flags = {}

		self.__timesource = timesource

	def __call__(self, *args, **kwargs):
		return self

	@property
	def store(self):
		return self.__store

	def push_contest(self, key, fn=None):
		self.__flags[key] = (self.CONTEST, fn)

	def time(self):
		return self.__timesource()

	def set(self, key, value, time=None):
		'''Stores the value under key, for the given amount of time

		Returns success'''
		logger.debug("'%s' = %s, for %s", str(key), str(value), str(time))

		entry = MockEntry(key, value, time, self)
		self.__store[key] = entry

		return True

	def add(self, key, value, time=None):
		'''Inserts the value under key only if it does not exist

		Returns success'''
		if key in self.__store and not self.__store[key].expired:
			logger.debug("ADD '%s' MISMATCH", key)
			return False

		token = self.__flags.pop(key, None)
		if token:
			# execute function if present
			if token[1]:
				token[1](self, key)
			logger.debug("ADD '%s' MISMATCH", key)
			return False

		logger.debug("ADD '%s' = %s, for %s", str(key), str(value), str(time))

		self.__store[key] = MockEntry(key, value, time, self)

		return True

	def cas(self, key, value, etag, time=None):
		'''Stores the value under key, but only if the given cas token
		is present for the existing value

		Returns success'''
		entry = self.__store.get(key)
		if entry and (entry.expired or entry.etag != etag):
			logger.debug("CAS '%s' MISMATCH", key)
			raise pylibmc.NotFound()

		token = self.__flags.pop(key, None)
		if token:
			if token[1]:
				token[1](self, key)
			logger.debug("CAS '%s' MISMATCH", key)
			raise pylibmc.NotFound()

		logger.debug("CAS '%s' = %s, for %s", str(key), str(value), str(time))

		self.__store[key] = MockEntry(key, value, time, self)

		return True

	def get(self, key, default=None):
		'''Retrieves the value from the store, or the default or None
		if there is no entry'''

		if key in self.__store:
			entry = self.__store[key]

			logger.debug("GET '%s', %s", str(key), str(entry))

			return entry.value

		logger.debug("GET '%s' MISS", str(key))

		return default or None

	def gets(self, key):
		'''Retrieves tuple of (value, cas token) from store,
		or (None, None) if there is no entry'''

		if key in self.__store:
			entry = self.__store[key]

			logger.debug("GETs '%s', %s (%s)", str(key), str(entry), str(entry.etag))

			return (entry.value, entry.etag,)

		logger.debug("GETs '%s' MISS", str(key))

		return (None, None,)

	def delete(self, key):
		'''Removes key from store, returning t/f flag for presence'''

		logger.debug("DEL %s", str(key))

		entry = self.__store.pop(key, None)
		return entry is not None
