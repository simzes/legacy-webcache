'''
(c) 2018 simzes
'''

import time
import datetime
from dateutil import tz

def application(environ, start_response):
    '''A wsgi application that returns an html page with the current unix
    time, with a link to a page and an embedded resource from another
    page.

    Used for testing server and client caching properties with
    different headers.

    Headers issued disable server-side and client-side caching
    (last-modified is the current time, expires is in the past, and
    cachecontrol and pragma disable caching)

    '''

    status = '200 OK'

    request_url = environ['REQUEST_URI']

    html = "<p>" + str(time.time()) + "</p>"
    if not request_url.startswith('/cacheme'):
        html += '<p><a href="/b">stuff link</a></p>'
        html += "<embed src=/cacheme/c>"

    start_response(status, [
        ('Content-Type', 'text/html; charset=UTF-8'),
        ('CacheControl', 'private, max-age=1, must-revalidate'),
        ('Pragma', 'no-cache'),
        ('Last-Modified', datetime.datetime.now(tz=tz.gettz('GMT')).strftime('%a, %d %b %Y %H:%M:%S GMT')),
        ('Expires', datetime.datetime(year=1997, month=7, day=5, hour=12, tzinfo=tz.gettz('GMT')).strftime('%a, %d %b %Y %H:%M:%S GMT')),
        ('Set-Cookie', 'random_test_cookie=' + str(time.time())),
        ])

    return [html]
