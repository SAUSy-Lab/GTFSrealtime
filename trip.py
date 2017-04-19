# documentation on the nextbus feed:
# http://www.nextbus.com/xmlFeedDocs/NextBusXMLFeed.pdf

import re, db, json
import map_api
from numpy import mean
import threading
import random
# testing...
import shapely.wkb
from shapely.geometry import Point

print_lock = threading.Lock()

class trip(object):
	"""The trip class provides all the methods needed for dealing
		with one observed trip/track. Classmethods provide two 
		different ways of instantiating."""

	def __init__(self,trip_id,block_id,direction_id,route_id,vehicle_id,last_seen):
		"""initialization method, only accessed by the @classmethod's below"""
		# set initial attributes
		self.trip_id = trip_id				# int
		self.block_id = block_id			# int
		self.direction_id = direction_id	# str
		self.route_id = route_id			# int
		self.vehicle_id = vehicle_id		# int
		self.last_seen = last_seen			# last vehicle report (epoch time)
		# initialize sequence
		self.seq = 1							# sequence which increments at each report
		# declare several vars for later in the matching process
		self.speed_string = ""				# str
		self.match_confidence = -1			# 0 - 1 real
		self.match_geometry = {}			# parsed geojson object
		self.stops = {}						# not set until process()
		self.segment_speeds = []			# reported speeds of all segments
		self.waypoints = []					# points on the finallized trip only
		# TODO testing...
		self.length = 0						# length in meters of current string
		self.vehicles = []					# ordered vehicle records
		self.ignored_vehicles = []			# discarded records
		self.problems = []					# running list of issues

	@classmethod
	def new(clss,trip_id,block_id,direction_id,route_id,vehicle_id,last_seen):
		"""create wholly new trip object, providing all paremeters"""
		# store instance in the DB
		db.insert_trip( trip_id, block_id, route_id, direction_id, vehicle_id )
		return clss(trip_id,block_id,direction_id,route_id,vehicle_id,last_seen)

	@classmethod
	def fromDB(clss,trip_id):
		"""construct a trip object from an existing record in the database"""
		(bid,did,rid,vid,last_seen) = db.get_trip(trip_id)
		return clss(trip_id,bid,did,rid,vid,last_seen)

	def process(self):
		"""A trip has just ended. What do we do with it?"""
		db.scrub_trip(self.trip_id)
#		db.sequence_vehicles(self.trip_id)
		db.update_vehicle_geoms(self.trip_id)

		# TODO testing shapely...
		
		# get vehicles and make geometry objects
		self.vehicles = db.shp_get_vehicles(self.trip_id)
		for v in self.vehicles:
			v['geom'] = shapely.wkb.loads(v['geom'],hex=True)
			v['ignore'] = False
		# calculate vector of segment speeds
		self.segment_speeds = self.get_segment_speeds()
		# check for very short trips
		if self.length < 0.8: # km
			return db.ignore_trip(self.trip_id,'too short')
		# check for errors and attempt to correct them
		while self.has_errors():
			# make sure it's still long enough to bother with
			if len(self.vehicles) < 3:
				return db.ignore_trip(self.trip_id,'error processing made too short')
			# still long enough to try fixing
			self.fix_error()
			# update the segment speeds for the next iteration
			self.segment_speeds = self.get_segment_speeds()
		# trip is clean, so store the cleaned line and begin matching
		self.match()

	def get_segment_speeds(self):
		"""return speeds (kmph) on the segments between vehicles
			non-ignored only and using shapely"""
		# iterate over segments (i-1)
		dists = []	# km
		times = []	# hours
		for i in range(1,len(self.vehicles)):
			v1 = self.vehicles[i-1]
			v2 = self.vehicles[i]
			# distance in kilometers
			dists.append( v1['geom'].distance(v2['geom'])/1000 )
			# time in hours
			times.append( (v2['time']-v1['time'])/3600 )
		# set the total distance
		self.length = sum(dists)
		# calculate speeds
		return [ d/t for d,t in zip(dists,times) ]


	def match(self):
		"""Match the trip to the road network, and do all the
			things that follow therefrom."""
		match = map_api.map_match(self.vehicles)
		# flag results with multiple matches for now until you can 
		# figure out exactly what is going wrong
		if match['code'] != 'Ok':
			return self.flag('match problem, code not "Ok"')
		if len(match['matchings']) > 1:
			return self.flag('more than one match segment')
		# get the matched points
		tracepoints = match['tracepoints']
		match = match['matchings'][0]
		# store the trip geometry
		db.add_trip_match(
			self.trip_id,
			match['confidence'],
			json.dumps(match['geometry'])
		)
		# is the match good enough to proceed with?
		if match['confidence'] < 0.5:
			print '\t',match['confidence'],', is too low'
		else:
			print '\t',match['confidence']
		# get the times for the waypoints from the vehicle locations
		times = db.get_waypoint_times(self.trip_id)
		# compare to the corresponding points on the matched line 
		for point,time in zip(tracepoints,times):
			# these are the matched points of the input cordinates
			try:
				self.waypoints.append({
					't':time,
					'm':db.locate_trip_point(
						self.trip_id,
						point['location'][0],	# lon
						point['location'][1]		# lat
					)
				})
			except:
				print '\t\t\twaypoint fail'
		# get the stops ( as a dict keyed by stop_id
		# with keys {'s':sequence,'m':measure,'d':distance}
		self.stops = db.get_stops(self.trip_id,self.direction_id)
		# we now have all the waypoints and all the stops and
		# can begin interpolating times, to be stored alongside the stops.
		num_times = True
		for stop_id in self.stops.keys():
			if self.stops[stop_id]['d'] < 20: # if close enough to be interpolated
				stop_time = self.interpolate_time(stop_id)
				if not stop_time: 
					continue
				# get the stop time and store it
				db.store_stop_time(
					self.trip_id,	# trip_id
					stop_id,			# stop_id
					stop_time		# epoch time
				)
				num_times += 1
		if num_times > 1:
			db.finish_trip(self.trip_id)
		else:
			db.ignore_trip(self.trip_id,'only one stop time estimated')
		return


	def ignore_vehicle(self,index):
		"""ignore a vehicle specified by the index"""
		v = self.vehicles.pop(index)
		self.ignored_vehicles.append(v)

	def flag(self,problem_description):
		"""record that something undesireable has occured"""
		self.problems.append(problem_description)


	def has_errors(self):
		"""see if the speed segments indicate that there are any 
			fixable errors by making the speed string and checking
			for fixeable patterns."""
		# convert the speeds into a string
		self.speed_string = ''.join([ 
			'x' if seg > 120 else 'o' if seg < 0.1 else '-'
			for seg in self.segment_speeds ])
		# do RegEx search for 'x' or 'oo'
		match_oo = re.search('oo',self.speed_string)
		match_x = re.search('x',self.speed_string)
		if match_oo or match_x:
			return True
		else:
			return False

	def fix_error(self):
		"""remove redundant points and fix obvious positional 
			errors using RegEx. Fixes one error each time it's 
			called: the first it finds"""
		# check for leading o's (stationary start)
		m = re.search('^oo*',self.speed_string)
		if m: # remove the first vehicle
			self.ignore_vehicle(0)
#			db.delete_vehicle( self.trip_id, 1 )
			return
		# check for trailing o's (stationary end)
		m = re.search('oo*$',self.speed_string)
		if m: # remove the last vehicle
			self.ignore_vehicle( len(self.speed_string) )
#			db.delete_vehicle( self.trip_id, len(self.speed_string)+1 )
			return
		# check for x near beginning, in first four segs
		m = re.search('^.{0,3}x',self.speed_string)
		if m: # remove the first vehicle
			self.ignore_vehicle(0)
#			db.delete_vehicle( self.trip_id, 1 )
			return
		# check for x near the end, in last four segs
		m = re.search('x.{0,3}$',self.speed_string)
		if m: # remove the last vehicle
			self.ignore_vehicle(len(self.speed_string))
#			db.delete_vehicle( self.trip_id, len(self.speed_string)+1 )
			return
		# check for two or more o's in the middle and take from after the first o
		m = re.search('.ooo*.',self.speed_string)
		if m:
			# remove the vehicle after the first o. This matches like '-oo-'
			# so we need to add 2 to the start position to remove the vehicle 
			# report from between the o's ('-o|o-')
			self.ignore_vehicle(m.span()[0]+1)
#			db.delete_vehicle( self.trip_id, m.span()[0]+2 )
			return
		# 'xx' in the middle, delete the point after the first x
		m = re.search('.xxx*',self.speed_string)
		if m:
			# same strategy as above
			self.ignore_vehicle(m.span()[0]+1)
#			db.delete_vehicle( self.trip_id, m.span()[0]+2 )
			return
		# lone middle x
		m = re.search('.x.',self.speed_string)
		if m:
			# delete a point either before or after a lone x
			i = m.span()[0]+1+random.randint(0,1)
			self.ignore_vehicle(i-1)
#			db.delete_vehicle( self.trip_id, i )
			return


	def interpolate_time(self,stop_id):
		"""get the time for a stop which is ordered by doing an interpolation
			on the trip times and locations. We already know the m of the stop
			and of the points on the trip/track"""
		stop_m = self.stops[stop_id]['m']
		# iterate over the segments of the trip, looking for the segment
		# which holds the stop of interest
		first = True
		for point in self.waypoints:
			if first:
				first = False
				m1 = point['m'] # zero
				t1 = point['t'] # time
				continue
			m2 = point['m']
			t2 = point['t']
			if m1 <= stop_m <= m2:	# intersection is at or between these points
				# interpolate the time
				if stop_m == m1:
					return t1
				percent_of_segment = (stop_m - m1) / (m2 - m1)
				additional_time = percent_of_segment * (t2 - t1) 
				return t1 + additional_time
			# create the segment for the next iteration
			m1,t1 = m2,t2
		print '\t\t\tstop thing failed??'
		return None


