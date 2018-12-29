'''
A wsgi wrapper around webcache.py

(c) 2018 simzes
'''

import logging

logging.basicConfig(filename='/usr/local/www/logs/wsgi.log', level=logging.DEBUG, format="%(levelname)s:%(asctime)-15s:%(thread)d %(message)s")
logging.info("Starting up")


from webcache import handle_application

def application(environ, start_response):
    return handle_application(environ, start_response)
