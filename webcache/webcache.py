'''
A small application for caching requests going to a web server

(c) 2018 simzes

See the README for detailed information.
'''

import pylibmc
import requests
import hashlib

import time
import datetime
from dateutil import tz
from random import randint

import logging
import sys

# how frequently a sleeping thread checks the cache for updates
SLEEP_POLL_INTERVAL = 0.5

# to sleep, thread picks a random number between 0 and some number equal to
# SLEEP_MULTIPLY_INTERVAL * the number of known, competing threads
SLEEP_MULTIPLY_INTERVAL = 5

# maximum sleep amount
SLEEP_MAX_SECONDS = 30

# maximum number of attempts to update the cache before bailing
UPDATE_MAX_ATTEMPTS = 20

# how long a cache metadata entry is valid
EXPIRE_SECS = 30

HTTP_HEADER_PREFIX = 'HTTP_'
HTTP_DATE_PARSE_FORMAT = '%a, %d %b %Y %H:%M:%S %Z'
HTTP_DATE_DISPLAY_FORMAT = '%a, %d %b %Y %H:%M:%S GMT'

gmt_tz = tz.gettz('GMT')

# headers that are removed from the server -> cache/client response
drop_headers = set([
    'Last-Modified',
    'Vary',
    'Server',
    'Keep-Alive',
    'Connection',
    'Transfer-Encoding',
    'Content-Encoding',
])

# flag for dropping responses from the server that don't have an OK status,
# and not caching them
DROP_NOT_OK_STATUS = True

# tuple or float passed to the requests library for conn/read timeout
REQUEST_TIMEOUT = (0.5, 15)

def parse_http_date(http_date_str):
    return datetime.datetime(*(time.strptime(http_date_str, HTTP_DATE_PARSE_FORMAT)[0:6]), tzinfo=gmt_tz)

def make_http_date(datetime_obj):
    return datetime_obj.strftime(HTTP_DATE_DISPLAY_FORMAT)

def get_request_headers(environ):
    '''cgi/wsgi http headers are encoded like: 'HTTP_CONTENT_LENGTH: <value>'

    To recover the original header name, we remove the "HTTP_" prefix,
    lowercase the field name, split on underscores, capitalize each
    split segment, and then rejoin with dashes
    '''
    http_headers = {}
    prefix_len = len(HTTP_HEADER_PREFIX)

    for cgi_header, value in environ.iteritems():
        if cgi_header.startswith(HTTP_HEADER_PREFIX):
            lc_fieldname = cgi_header[prefix_len:].lower()
            field_segments = lc_fieldname.split('_')
            steptyped_segments = [f.capitalize() for f in field_segments]
            header_name = '-'.join(steptyped_segments)

            http_headers[header_name] = value
    return http_headers

def sha256_digest(content):
    sha2 = hashlib.sha256()
    sha2.update(content)
    return sha2.digest()

class WSGIRequest(object):
    '''Object for encapsulating a WSGI request'''
    def __init__(self, request_url, request_headers, request_time):
        self._time = request_time
        self._headers = request_headers
        self._url = request_url

    def __str__(self):
        return "WSGIRequest[url: %s, headers: %s]" % (self._url, str(self._headers),)

    @property
    def url(self):
        return self._url

    @property
    def headers(self):
        return self._headers

    @property
    def time(self):
        return self._time

class WSGIResponse(object):
    '''Object for encapsulating a WSGI response'''

    def __init__(self):
        self._headers = []
        self._content = []

    def __str__(self):
        return "WSGIResponse[status: %s, headers: %s]" % (self._status, self._headers,)

    @property
    def status(self):
        return self._status

    @property
    def headers(self):
        return self._headers

    @property
    def content(self):
        return self._content

    def add_header(self, header_name, header_value):
        self.headers.append((header_name, header_value,))

    def set_content_body(self, body):
        self._content = [body]

    @staticmethod
    def from_cache_metadata(cache_metadata):
        response = WSGIResponse()

        response.add_header('Last-Modified', cache_metadata.last_modified)
        for header, value in cache_metadata.content_entry.headers.iteritems():
            if header not in drop_headers:
                response.add_header(header, value)

        response._status = cache_metadata.content_entry.status
        response.set_content_body(cache_metadata.content_entry.content)

        return response

    @staticmethod
    def from_internal_error():
        response = WSGIResponse()
        response._status = "500 Internal Server Error"

        return response

class EntryMetadata(object):
    '''Metadata about a cache entry.

    Stored data holds the hash/time fields for the last content and accesses,
    the location of the content itself, and fields for thread contests (see
    note above).

    Non-stored data handles holding onto the cache client, any consistency tokens
    from reads (ensure that when we update, we CAS or insert the metadata entry),
    and lazy-loading of the associated content.

    If there is a cache miss, the entry is constructed anew from a server
    response (from_server_response factory). If there is a cache hit, or if
    the cache has an entry that may be expired, we update the metadata to
    reflect the new server content or the new access details.
    '''

    _data_fields = set([
        "valid",
        "session",
        "url",
        "fetched",
        "last_modified",
        "sha256_digest",
        "reservation",
        "last_noted",
        "content_key"
    ])

    def __init__(self):
        self._data = {}
        self._content_entry = None
        self._mc_client = None
        self._etag = None

    def __str__(self):
        return str(self._data)

    def __getattr__(self, attr):
        return self._data[attr]

    def __setattr__(self, attrname, value):
        '''sets internal data object or properties'''
        if attrname in self._data_fields:
            self._data[attrname] = value
        else:
            object.__setattr__(self, attrname, value)

    @property
    def metadata_key(self):
        return EntryMetadata.make_metadata_key(self.url)

    @staticmethod
    def make_metadata_key(url):
        return "metadata_%s" % (url,)

    @staticmethod
    def make_content_key(url, reservation_token):
        session, reservation = reservation_token
        return "body_%s_%f-%d" % (url, session, reservation,)

    @property
    def content_entry(self):
        '''Lazily loads the content entry from cache, if constructed to
        reference cache content, or returns the known content entry,
        if constructed from a server response'''
        if self._content_entry is None:
            self._content_entry = EntryContent.from_cache(self)
        return self._content_entry

    def store_metadata(self):
        '''Commits this metadata to cache, using the CAS token from loading,
        or inserting if the entry doesn't exist

        Returns whether the operation worked
        '''
        logging.debug("cache[%s] = %s", self.metadata_key, self._data)

        if self._etag is not None:
            try:
                return self._mc_client.cas(self.metadata_key, self._data, self._etag)
            except pylibmc.NotFound:
                # entry could have been evicted since creation--try insert once
                pass
        return self._mc_client.add(self.metadata_key, self._data)

    def delete_metadata(self):
        '''Removes the metadata entry from the cache'''
        logging.debug("cache[%s] delete", self.metadata_key)
        self._mc_client.delete(self.metadata_key)

    @staticmethod
    def from_cache_or_none(mc_client, url):
        '''Build an EntryMetadata object with the contents from cache, if any.

        Returns None if no entry could be found.
        '''
        cache_entry, etag = mc_client.gets(EntryMetadata.make_metadata_key(url))
        if cache_entry is None:
            return None

        entry = EntryMetadata()
        entry._mc_client = mc_client
        entry._data = cache_entry
        entry._etag = etag

        return entry

    @staticmethod
    def new_reservation(mc_client, url):
        '''
        Build an EntryMetadata object to insert into the cache when there is
        no metadata entry, as a placeholder for a thread that is making an
        update request to the server.

        Having a placeholder lets us (more) accurately count the number of
        threads that are competing, as the reservation field is incremented
        with each update.
        '''

        entry = EntryMetadata()

        entry._mc_client = mc_client
        entry._etag = None
        entry.valid = False

        entry.url = url
        entry.sha256_digest = None

        entry.session = unixtime()
        # reservation is one, as this thread is the first in line
        entry.reservation = 1
        entry.last_noted = 0

        return entry

    @staticmethod
    def from_server_response(mc_client, url, content_entry):
        '''Builds a new EntryMetadata object from a response object from the
        server'''

        entry = EntryMetadata()

        entry._mc_client = mc_client
        entry._etag = None
        entry.valid = True

        entry.url = url
        entry.fetched = entry.session = unixtime()
        entry.last_modified = EntryMetadata.time_or_last_modified_header(entry.fetched, content_entry)
        entry.sha256_digest = sha256_digest(content_entry.content)
        entry.reservation = 0
        entry.last_noted = 0

        entry.content_key = content_entry.content_key
        entry._content_entry = content_entry

        return entry

    def update_for_server_response(self, content_entry):
        '''Updates an existing cache metadata entry with updates from a new
        server request'''
        update_time = unixtime()

        # common fields for updating a reservation and a valid cache entry
        self.fetched = update_time
        self.last_noted = self.reservation

        self.valid = True
        self.content_key = content_entry.content_key

        if self.sha256_digest != content_entry.digest:
            # contents have changed; need to update hash, modified date, and key
            self.last_modified = EntryMetadata.time_or_last_modified_header(update_time, content_entry)
            self.sha256_digest = content_entry.digest
            self.content_key = content_entry.content_key

        self._content_entry = content_entry

    @staticmethod
    def time_or_last_modified_header(unixtime, content_entry):
        '''The given unixtime, or the content_entry's last-modified header,
        whichever is older'''
        unixtime_datetime = datetime.datetime.fromtimestamp(unixtime, tz=gmt_tz)

        if 'Last-Modified' in content_entry.headers:
            last_modified = content_entry.headers['Last-Modified']
            last_modified_datetime = parse_http_date(last_modified)
            return make_http_date(min(unixtime_datetime, last_modified_datetime))

        return make_http_date(unixtime_datetime)

class EntryContent(object):
    '''Object for representing a server's response at rest in the cache

    Lazily computes the sha256 digest of the response's content
    '''

    def __init__(self):
        self.__digest = None

    @property
    def digest(self):
        if self.__digest is None:
            self.__digest = sha256_digest(self.content)
        return self.__digest

    @property
    def content_key(self):
        return self._content_key

    @property
    def status(self):
        return self._status

    @property
    def url(self):
        return self._url

    @property
    def headers(self):
        return self._headers

    @property
    def content(self):
        return self._content

    def store_content(self):
        '''Commits the entry to cache, returning success'''
        cache_entry = {}
        cache_entry['status'] = self._status
        cache_entry['url'] = self._url
        cache_entry['headers'] = self._headers
        cache_entry['content'] = self._content

        logging.debug("cache[%s] = [...]", self._content_key)
        return self._mc_client.set(self._content_key, cache_entry)

    def delete_content(self):
        logging.debug("cache[%s] deleted", self._content_key)
        self._mc_client.delete(self._content_key)

    @staticmethod
    def from_cache(entry_metadata):
        cache_key = entry_metadata.content_key
        cache_entry = entry_metadata._mc_client.get(cache_key)
        if cache_entry is None:
            return None

        entry = EntryContent()
        entry._content_key = cache_key

        entry._status = cache_entry['status']
        entry._url = cache_entry['url']
        entry._headers = cache_entry['headers']
        entry._content = cache_entry['content']

        return entry

    @staticmethod
    def from_server_response(response, url, mc_client, reservation_token):
        entry = EntryContent()
        entry._mc_client = mc_client
        entry._content_key = EntryMetadata.make_content_key(url, reservation_token)

        entry._status = '%d %s' % (response.status_code, response.reason,)
        entry._url = url
        entry._headers = response.headers
        entry._content = response.content

        return entry

class ConsistencyError(Exception):
    '''Exception class for handling inability to update cache within
    a reasonable number of tries'''
    pass

def handle_application(environ, start_response):
    wsgi_request = WSGIRequest(
        request_url=environ['REQUEST_URI'],
        request_headers=get_request_headers(environ),
        request_time=unixtime()
        )

    logging.info("Received request: %s", wsgi_request)

    try:
        wsgi_response = handle_request(wsgi_request)
        logging.info("Issuing response: %s", wsgi_response)
    except ConsistencyError:
        logging.warn("Couldn't update cache due to contention--bailing early")
        wsgi_response = WSGIResponse.from_internal_error()
    # other exceptions are caught and logged by the wsgi handler,
    # into the apache error logs

    start_response(wsgi_response.status, wsgi_response.headers)

    return wsgi_response.content

def handle_request(wsgi_request):
    '''Handles a request, converting a WSGIRequest to a WSGIResponse'''
    mc = _open_client()

    # check if we can serve the request from cache
    cached_response = check_for_cache_response(mc, wsgi_request)

    if cached_response:
        logging.debug("Serving from cache")
        return cached_response

    # can't serve from the cache -- compete for cache update
    won, reservation_token = compete_for_cache_update(wsgi_request, mc)
    if not won:
        # check cache again to see if a competing thread has updated the entry
        cached_response = check_for_cache_response(mc, wsgi_request)
        if cached_response:
            logging.debug("Serving parallel-update from cache")
            return cached_response

    logging.debug("Can't serve from cache--issuing new request to the origin")

    # update the cache and fulfill the request with our own request to the server
    server_response = _issue_server_request(wsgi_request)
    cache_metadata = update_cache(mc, wsgi_request, server_response, reservation_token)

    return WSGIResponse.from_cache_metadata(cache_metadata)

def check_for_cache_response(mc_client, wsgi_request, cache_metadata=None):
    '''
    Checks the cache to see if a response can be served from the current cache
    contents.

    Takes an optional EntryMetadata (cache_metadata) object, and retrieves and
    makes its own otherwise.

    Returns a WSGIResponse object if there is a valid response. Otherwise,
    returns None.

    A request can be served from cache if: 
    --the metadata is valid and the request headers match a
        client-side cache condition
    --the metadata is valid and the object's body is present
    '''
    if cache_metadata is None:
        cache_metadata = EntryMetadata.from_cache_or_none(mc_client, wsgi_request.url)

    logging.debug("Checking cache metadata for url: %s", wsgi_request.url)

    if cache_metadata is None:
        logging.debug("No cache entry")
        return None

    if not cache_metadata.valid:
        logging.debug("No valid cache entry")
        return None

    if wsgi_request.time > (cache_metadata.fetched + EXPIRE_SECS):
        logging.debug("Expired cache entry; can't serve")
        return None

    # check for client-side caching headers
    if 'If-Modified-Since' in wsgi_request.headers:
        client_datetime = parse_http_date(wsgi_request.headers['If-Modified-Since'])
        cache_datetime = parse_http_date(cache_metadata.last_modified)
        if client_datetime >= cache_datetime:
            logging.debug("Client's If-Modified-Since valid for client-side cache")
            response = WSGIResponse()
            response._status = '304 Not Modified'

            return response
        else:
            logging.debug("Client's If-Modified-Since too old for client-side cache")

    if cache_metadata.content_entry is not None:
        logging.debug("Have valid cache body")
        return WSGIResponse.from_cache_metadata(cache_metadata)

    logging.debug("No cache body; can't serve from cache")

    return None

def compete_for_cache_update(wsgi_request, mc_client):
    '''Run to coordinate updates whenever a request cannot be served from cache

    Updates are coordinated by adding a new entry, or incrementing the
    reservation field in the cache metadata, with the following logic:

    --if no metadata exists in the cache, then we create a new metadata entry,
    with the reservation and last_noted fields both set to 0, and with valid
    set to false to denote a reservation, and insert it into the cache.

    --if metadata exists, then we increment the reservation field in
    the read metadata, and do a compare-and-swap operation to update
    the metadata.

    If we win (reservation <= last_noted + 1), then we return immediately. If
    we lose, then we sleep for some backoff period, where the backoff amount
    is determined by the difference between the reservation and last_noted
    fields.

    During sleep, the thread will poll the cache entry at some interval to see
    if it's changed and become valid
    '''
    cache_metadata, won = update_reservation(mc_client, wsgi_request.url)
    reservation_token = (cache_metadata.session, cache_metadata.reservation,)

    if won:
        logging.debug("Won cache update, with reservation: %s", reservation_token)
        return (True, reservation_token,)

    # backoff by picking a random time between 0 and backoff *
    # SLEEP_MULTIPLY_SECONDS, up to a maximum of SLEEP_MAX_SECONDS
    backoff = (cache_metadata.reservation - cache_metadata.last_noted)
    stop = unixtime() + randint(0, min(backoff * SLEEP_MULTIPLY_INTERVAL, SLEEP_MAX_SECONDS))

    logging.debug("Lost cache update, backing off until: %d, now: %d, reservation: %s", int(stop), int(unixtime()), reservation_token)

    while stop > unixtime():
        time.sleep(
            min(
                SLEEP_POLL_INTERVAL,
                max(stop - unixtime(), 0)
            ))

        cache_metadata = EntryMetadata.from_cache_or_none(mc_client, wsgi_request.url)
        if (cache_metadata is None) or cache_metadata.valid:
            break

    logging.debug("Finished cache backoff")

    return (False, reservation_token,)

def update_reservation(mc_client, url):
    '''Updates the metadata in cache, s.t. the reservation field is
    incremented if the entry exists, or set as reservation = last_noted = 0,
    if it doesn't.

    Returns the (EntryMetadata, won flag) tuple.
    '''

    for _ in range(UPDATE_MAX_ATTEMPTS):
        cache_metadata = EntryMetadata.from_cache_or_none(mc_client, url)
        if cache_metadata:
            cache_metadata.reservation += 1
        else:
            cache_metadata = EntryMetadata.new_reservation(mc_client, url)

        if cache_metadata.store_metadata():
            won = (cache_metadata.reservation == cache_metadata.last_noted + 1)
            return (cache_metadata, won,)

    raise ConsistencyError()

def update_cache(mc_client, wsgi_request, server_response, reservation_token):
    '''Tries to update the cache to reflect the given server response.

    If the cache has a valid entry, then we use this.

    If there is no cache content, or if the content differs, then the
    server response is stored into the cache as metadata and content.

    If the content is the same as in the cache, then we don't update
    the body, but preserve the existing "Last-Modified" header and the
    link to the existing content (held in the contents key). Note that
    the reservation count, which is also used as a means for thread
    contests/backoff, is only converted to a content_key if the content
    is new.

    Finally, we bail after some number of tries to update the cache
    '''
    content_entry = EntryContent.from_server_response(server_response, wsgi_request.url, mc_client, reservation_token)

    if DROP_NOT_OK_STATUS and (not server_response.ok):
        logging.debug("Server response not OK -- invalidating cache")
        cache_metadata = EntryMetadata.from_server_response(mc_client, wsgi_request.url, content_entry)

        # delete metadata as a way of notifying other, waiting threads that the
        # blocking thread has given up
        cache_metadata.delete_metadata()

        return cache_metadata

    if not content_entry.store_content():
        raise ConsistencyError()

    for _ in range(UPDATE_MAX_ATTEMPTS):
        cache_metadata = EntryMetadata.from_cache_or_none(mc_client, wsgi_request.url)
        if cache_metadata:
            if check_for_cache_response(mc_client, wsgi_request, cache_metadata=cache_metadata):
                # can already serve from cache--return response
                # delete server body we stored unnecessarily
                content_entry.delete_content()
                return cache_metadata
            # have entry, but need to update metadata with server response to make valid
            cache_metadata.update_for_server_response(content_entry)
        else:
            # no existing entry--insert new one
            cache_metadata = EntryMetadata.from_server_response(mc_client, wsgi_request.url, content_entry)
        if cache_metadata.store_metadata():
            return cache_metadata

    raise ConsistencyError()

def _open_client():
    '''Creates a memcached client using tcp; nodelay is set and CAS behaviors are needed'''
    return pylibmc.Client(
        ["127.0.0.1"],
        binary=True,
        behaviors = {"tcp_nodelay": True, "cas": True}
    )

def _issue_server_request(wsgi_request):
    logging.debug("Issuing request to origin server: %s", wsgi_request)

    response = requests.get("http://127.0.0.1" + wsgi_request.url, headers=wsgi_request.headers)
    logging.debug("Server response--status: %d, reason: %s", response.status_code, response.reason)

    return response

def unixtime():
    return time.time()
