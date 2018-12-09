'''
(c) 2018 simon's bench
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

    html = str(time.time()) + '\n'

    html += '\n'.join(["%s: %s" % (k, v,) for k, v in environ.iteritems()])

    start_response(status, [
        ('Content-Type', 'text/plain'),
        ('CacheControl', 'private, must-revalidate, max-age=1'),
        ('Pragma', 'no-cache'),
        ('Last-Modified', datetime.datetime.now(tz=tz.gettz('GMT')).strftime('%a, %d %b %Y %H:%M:%S GMT')),
        ('Expires', datetime.datetime(year=1997, month=7, day=5, hour=12, tzinfo=tz.gettz('GMT')).strftime('%a, %d %b %Y %H:%M:%S GMT')),
        ])

    return [html]
