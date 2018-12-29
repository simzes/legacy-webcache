import unittest
import webcache
import pylibmc
import collections

import fixtures.memcache_test_client
import fixtures.server_mockout
import fixtures.time_mockout

import logging
import sys

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
logger = logging.getLogger(__name__)

class TestWebcache(unittest.TestCase):

	def __mock_start_response(self, status, headers):
		'''capture function for wsgi start_response function stores results locally'''
		if self.__response_started:
			raise RuntimeError("start_response called twice")

		self.__response_started = True
		self.__response_status = status
		self.__response_headers = collections.defaultdict(lambda: [])

		for header in headers:
			logging.info("header: %s", header)
			headername, headervalue = header
			self.__response_headers[headername].append(headervalue)

	def __pack_http_headers(self, headers):
		'''formats the given headers for the wsgi environment'''
		result = {}
		for header_name, header_value in headers.iteritems():
			underscore_name = header_name.replace('-', '_')
			result[('HTTP_' + underscore_name).upper()] = header_value

		return result

	def __http_date(self):
		import datetime
		datetime_obj = datetime.datetime.fromtimestamp(self._time_mockout.replacement_unixtime(), tz=webcache.gmt_tz)
		return webcache.make_http_date(datetime_obj)

	def make_overlay_request(self, url, headers):
		'''make the request into the webcache'''
		environ = {}
		environ['REQUEST_URI'] = url
		environ.update(self.__pack_http_headers(headers))

		logger.info("Making overlay request\n========")

		response_content = self.__response_content = webcache.handle_application(environ, self.__mock_start_response)

		logger.info("\n========\nOverlay request finished\n========")
		return response_content

	def setUp(self):
		logging.info("setting up")

		self.__response_started = False

		time_mockout = self._time_mockout = fixtures.time_mockout.TimeMockout()
		webcache.unixtime = time_mockout.replacement_unixtime

		webcache._open_client = self._mc_client = fixtures.memcache_test_client.MockPylibmcClient(time_mockout.replacement_unixtime)

		server_data = self._server_data = fixtures.server_mockout.ServerData()
		webcache._issue_server_request = server_data.replacement_issue_request

	def tearDown(self):
		logging.info("tearing down")

		webcache._open_client = None
		webcache._issue_server_request = None

	def assertOverlayResponseEqual(self, status=None, headers=None, content=None):
		'''check the result of the cache's response'''
		if status is not None:
			self.assertEqual(self.__response_status, status)

		if headers is not None:
			self.assertEqual(self.__response_headers, headers)

		if content is not None:
			self.assertEqual(self.__response_content, [content])

	def assertCacheEqual(self, the_url, **kwargs):
		'''checks that for the given url, all key-value pairs in kwargs
		match in the content body'''
		metadata_key = webcache.EntryMetadata.make_metadata_key(the_url)
		metadata_body = self._mc_client.get(metadata_key)

		self.assertIsNotNone(metadata_body)

		content_key = metadata_body['content_key']
		content_body = self._mc_client.get(content_key)

		self.assertIsNotNone(content_body)

		for key, value in kwargs.iteritems():
			self.assertEqual(content_body[key], value)

	def get_metadata_fields(self, url, *keys):
		'''Retrieves the key, value pairs table for the given
		url and keys, from the metadata entry'''
		metadata_key = webcache.EntryMetadata.make_metadata_key(url)
		metadata_body, _ = self._mc_client.gets(metadata_key)

		result = {}
		for key in keys:
			if key in metadata_body:
				result[key] = metadata_body[key]

		return result

	def assertMetadataEqual(self, the_url, **kwargs):
		'''checks that for the given url, all key-value pairs in kwargs
		match in the metadata body'''
		metadata_key = webcache.EntryMetadata.make_metadata_key(the_url)
		metadata_body = self._mc_client.get(metadata_key)

		self.assertIsNotNone(metadata_body)

		for key, value in kwargs.iteritems():
			self.assertEqual(metadata_body[key], value)

	def test_simple_get(self, content=None, headers=None):
		'''tests that a 200 ok response from the server is passed through
		and stored into the cache

		Used in other tests, so is fairly minimal'''
		if content is None:
			content = "stuff"
		if headers is None:
			headers = {}

		server_response = fixtures.server_mockout.MockResponse(
			status_code=200,
			reason="OK",
			content=content,
			headers=headers,
			)
		self._server_data.push_response('/url1', server_response)

		self.make_overlay_request('/url1', {})

		self.assertOverlayResponseEqual(status="200 OK", content=content)
		self.assertCacheEqual('/url1', content=content)

	def test_simple_get_metadata(self):
		'''tests that a simple get sets up the metadata correctly'''
		self.test_simple_get()
		self.assertMetadataEqual('/url1',
			valid=True,
			url='/url1',
			reservation=1,
			last_noted=1,
			)

	def test_simple_get_content(self):
		'''tests that a simple get sets up the content in the cache correctly'''
		self.test_simple_get(content="other stuff")
		self.assertCacheEqual('/url1',
			url='/url1',
			status="200 OK",
			headers={},
			content="other stuff"
			)

	def test_last_modified_retained(self):
		'''tests that a last-modified header passed through the server makes
		it through to the client, and that it is used in the cache'''
		http_date = self.__http_date()

		self.test_simple_get(headers={'Last-Modified': http_date})

		self.assertEqual(self.__response_headers['Last-Modified'], [http_date])
		self.assertMetadataEqual('/url1',
			last_modified=http_date
			)

	def test_dropped_get(self):
		'''tests that a not ok response from the server is passed through
		but not stored into the cache'''
		server_response = fixtures.server_mockout.MockResponse(
			status_code=500,
			reason="UNAVAILABLE",
			)
		self._server_data.push_response('/url1', server_response)

		self.make_overlay_request('/url1', {})

		# check status of the response matches
		self.assertOverlayResponseEqual(status="500 UNAVAILABLE", content='')

		# check no cache entry
		self.assertIsNone(self._mc_client.get(webcache.EntryMetadata.make_metadata_key('/url1')))

	def test_expired_get_same_content(self):
		'''tests that the cache metadata gets updated correctly when an
		expired entry is refetched with the same content

		last-modified is advanced in the server's response and the cache
		is checked to see if it remains the same'''

		self.test_simple_get()
		self.__response_started = False

		# hang onto metadata fields from first entry
		metadata_fields = self.get_metadata_fields('/url1',
			'last_modified',
			'sha256_digest',
			'session',
			)

		# run clock forward
		self._time_mockout.add_delta(60)

		# make request, with last-modified date set to current time
		self.test_simple_get(headers={'Last-Modified': self.__http_date()})

		# compare new and old metadata fields
		new_metadata_fields = self.get_metadata_fields('/url1',
			'last_modified',
			'sha256_digest',
			'session'
			)
		self.assertEqual(
			metadata_fields,
			new_metadata_fields
			)

		self.assertMetadataEqual('/url1',
			valid=True,
			last_noted=2,
			reservation=2
			)

	def test_expired_get_different_content(self):
		'''tests that the cache metadata gets updated when an expired
		entry is refetched, but the content differs'''
		self.test_simple_get()
		self.__response_started = False

		# hang onto metadata fields from first entry
		metadata_fields = self.get_metadata_fields('/url1',
			'last_modified',
			'sha256_digest',
			'session',
			)

		self._time_mockout.add_delta(60)
		http_date = self.__http_date()
		self.test_simple_get(content="other stuff", headers={'Last-Modified': http_date})

		# compare new and last ones--only the session should be the same
		new_metadata_fields = self.get_metadata_fields('/url1',
			'last_modified',
			'sha256_digest',
			'session'
			)

		self.assertNotEqual(
			metadata_fields['last_modified'],
			new_metadata_fields['last_modified']
			)
		self.assertEqual(
			new_metadata_fields['last_modified'],
			http_date
			)
		self.assertEqual(self.__response_headers['Last-Modified'], [http_date])

		self.assertNotEqual(
			metadata_fields['sha256_digest'],
			new_metadata_fields['sha256_digest']
			)

		self.assertEqual(
			metadata_fields['session'],
			new_metadata_fields['session']
			)

		self.assertMetadataEqual('/url1',
			valid=True,
			last_noted=2,
			reservation=2
			)

	def test_update_contention_loss(self):
		'''tests that a thread correctly handles losing a contest to
		update an entry, when it should fetch from the origin'''
		def insert_metadata_reservation(memcache_mockout, cache_key):
			# set the metadata entry when it is requested by the exercised thread
			entry = webcache.EntryMetadata.new_reservation(memcache_mockout, '/url1')
			entry.store_metadata()

		self._mc_client.push_contest('metadata_/url1', fn=insert_metadata_reservation)

		self.test_simple_get()

		self.assertMetadataEqual('/url1',
			valid=True,
			url='/url1',
			reservation=2,
			last_noted=2)

	def test_update_contention_loss_with_update_fulfilled(self):
		'''same as above, but with the losing thread finding that
		the server request was already fulfilled'''

		content = "competing stuff"

		def insert_metadata_reservation(memcache_mockout, cache_key):
			server_response = fixtures.server_mockout.MockResponse(
				status_code=200,
				reason="OK",
				content=content
			)

			content_entry = webcache.EntryContent.from_server_response(
				server_response,
				'/url1',
				memcache_mockout,
				(self._time_mockout.replacement_unixtime(), 1)
			)
			content_entry.store_content()

			entry = webcache.EntryMetadata.from_server_response(memcache_mockout, '/url1', content_entry)

			# make exercised thread think it's lost--from_server_response sets up a new entry, and the
			# first thread to update will win
			entry.reservation = 1

			entry.store_metadata()

		self._mc_client.push_contest('metadata_/url1', fn=insert_metadata_reservation)

		self.test_simple_get(content=content)

		self.assertMetadataEqual('/url1',
			valid=True,
			url='/url1',
			reservation=2,
			last_noted=0)

if __name__ == "__main__":
	unittest.main()
