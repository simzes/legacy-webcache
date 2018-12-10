'''
(c) 2018 simnzes
'''

import time
import datetime
from dateutil import tz

def application(environ, start_response):
    '''A wsgi application that returns the current unix time,
    with headers that disable server-side and client-side caching
    (last-modified is the current time, expires is in the past,
    and cachecontrol and pragma disable caching)
    '''

    status = '200 OK'

    request_url = environ['REQUEST_URI']

    html = "<p>" + str(time.time()) + "</p>"
    if not request_url.startswith('/cacheme'):
        html += '<p><a href="/b">stuff link</a></p>'
        html += "<embed src=/cacheme/c>"

    start_response(status, [
        ('Content-Type', 'text/html; charset=UTF-8'),
	('Content-Length', str(len(html.encode("utf8")))),
        ('Set-Cookie', 'random_test_cookie=' + str(time.time())),

        ('CacheControl', 'private, max-age=1, must-revalidate'),
        ('Pragma', 'no-cache'),

	# l-m some time ago
        # ('Last-Modified', (datetime.datetime.now(tz=tz.gettz('GMT')) - datetime.timedelta(hours=30)).strftime('%a, %d %b %Y %H:%M:%S GMT')),

	# l-m a little while ago
        # ('Last-Modified', (datetime.datetime.now(tz=tz.gettz('GMT')) - datetime.timedelta(seconds=10)).strftime('%a, %d %b %Y %H:%M:%S GMT')),

	# l-m just now
        ('Last-Modified', datetime.datetime.now(tz=tz.gettz('GMT')).strftime('%a, %d %b %Y %H:%M:%S GMT')),

	# expires in the future
        # ('Expires', (datetime.datetime.now(tz=tz.gettz('GMT')) + datetime.timedelta(days=2)).strftime('%a, %d %b %Y %H:%M:%S GMT')),

	# expires at static, future date
        # ('Expires', datetime.datetime(year=2018, month=12, day=5, hour=18, tzinfo=tz.gettz('GMT')).strftime('%a, %d %b %Y %H:%M:%S GMT')),

	# expires in the past
        ('Expires', datetime.datetime(year=1997, month=7, day=5, hour=12, tzinfo=tz.gettz('GMT')).strftime('%a, %d %b %Y %H:%M:%S GMT')),

        ])

    return [html]
