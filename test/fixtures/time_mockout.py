'''Fixture for replacing the unixtime() function, a stand-in for time.time()'''

import time

class TimeMockout(object):
	'''a time object that returns the current time, plus or minus some
	delta.

	Allows a test to advance the clock without waiting'''

	def __init__(self):
		self._delta = 0

	def replacement_unixtime(self):
		return time.time() + self._delta

	def add_delta(self, delta):
		self._delta += delta
