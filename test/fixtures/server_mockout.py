'''module for replacing webcache._issue_server_request function and server
representation with a mocked-out one
'''

class MockResponse(object):

	def __init__(self, status_code=None, reason=None, content=None, headers=None):
		if status_code is None:
			status_code = 200
		if reason is None:
			reason = ""
		if content is None:
			content = ""
		if headers is None:
			headers = {}

		self._status_code = status_code
		self._reason = reason
		self._headers = headers
		self._content = content

	@property
	def status_code(self):
		return self._status_code

	@property
	def ok(self):
		return self.status_code < 400

	@property
	def reason(self):
		return self._reason

	@property
	def headers(self):
		return self._headers

	@property
	def content(self):
		return self._content

class ServerData(object):
	'''An object for managing url -> response mappings

	Each entry in the table is keyed by url, and maps to
	a queue of mock requests.Response objects
	'''

	def __init__(self):
		self.__table = {}

	def push_response(self, url, mock_response):
		queue = self.__table.get(url)
		if queue is None:
			queue = self.__table[url] = []

		queue.append(mock_response)

	def poll_response(self, url):
		queue = self.__table.get(url)
		if not queue:
			raise RuntimeError("No server response set for %s" % (url,))

		return queue.pop(0)

	def replacement_issue_request(self, wsgi_request):
		return self.poll_response(wsgi_request.url)
